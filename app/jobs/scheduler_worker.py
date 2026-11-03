"""毎朝7:00（Asia/Tokyo）定期実行および停止後補完取得ワーカー。

目的（Why）:
    FastAPIのWebプロセスと分離された独立プロセスとして、毎朝7:00（日本時間）に
    ニュース取得・分析を自動実行し、Windows PCの停止・スリープ時には次回起動時に
    前回成功時点以降の未取得分を補完する（指示書 §1, §20）。
仕様（What）:
    - `run_scheduled_digest_once`: 二重実行防止ロック・エラー時再試行付きの定期更新実行
    - `run_startup_catchup`: 起動時の未取得分補完チェック・実行
    - `create_scheduler`: APScheduler の CronTrigger（07:00 Asia/Tokyo）構成
前提条件・依存関係（Assumption / Dependencies）:
    apscheduler>=3.10, `app.core.config`, `app.core.database`, `app.services.digest_orchestrator`
入出力（I/O）:
    入力: 実行モード・リトライ上限設定
    出力: 実行ステータス辞書
注意点（Caution）:
    無限リトライは禁止し、`max_retries`（既定2回）以内で停止して前回結果を保持すること（指示書 §12.3, §20）。
今後の拡張ポイント（Future Work）:
    24時間ごとの無料モデル一覧・料金スナップショット自動更新ジョブの追加。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 2-4）
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import session_scope
from app.core.logging import get_logger, mask_sensitive_text
from app.models import init_all_tables
from app.models.run import RUN_TYPE_SCHEDULED
from app.repositories.setting_repo import SettingRepository
from app.services.digest_orchestrator import execute_digest_run, run_catchup_if_needed
from app.services.rss_service import JST_ZONE

# =============================================================================
# 設定値ブロック（スケジューラ・再試行パラメータ）
# =============================================================================

DEFAULT_WORKER_MAX_RETRIES: int = 2
RETRY_BACKOFF_SECONDS: float = 1.0
MISFIRE_GRACE_TIME_SECONDS: int = 3600

logger = get_logger(__name__)


def run_scheduled_digest_once(
    session: Session | None = None,
    max_retries: int = DEFAULT_WORKER_MAX_RETRIES,
    retry_delay_seconds: float = RETRY_BACKOFF_SECONDS,
) -> dict[str, Any]:
    """定期更新ジョブを1回実行する（二重実行防止ロックおよび回数制限付き再試行を含む：指示書 §20）。

    解説：
        1. 初回承認（`is_initial_approved`）および自動更新（`auto_update_enabled`）が
           有効であるか確認する。
        2. 一時的な通信エラー等が発生した場合は、`max_retries`（既定2回）まで待機後に再試行する。
        3. 他のプロセスが既に実行中の場合は `status="locked"` を返して二重実行を防止する。

    Args:
        session (Session | None): テスト等で外部注入するDBセッション。省略時は `session_scope()` を使用。
        max_retries (int): 失敗時の最大再試行回数（0〜5）。
        retry_delay_seconds (float): 再試行時の待機秒数。

    Returns:
        dict[str, Any]: `status`, `attempts`, `run_id` 等を含む実行結果辞書。

    Side Effects:
        RSS取得・DB保存・実行履歴更新を行う。
    """
    bounded_retries = max(0, min(int(max_retries), 5))

    def _run_with_session(db: Session) -> dict[str, Any]:
        setting = SettingRepository(db).get_or_create()
        if not setting.is_initial_approved or not setting.auto_update_enabled:
            return {
                "status": "skipped_disabled",
                "attempts": 0,
                "run_id": None,
            }

        last_err: str | None = None
        for attempt in range(1, bounded_retries + 2):
            try:
                res = execute_digest_run(session=db, run_type=RUN_TYPE_SCHEDULED)
                if res.get("status") == "locked":
                    return {
                        "status": "locked",
                        "attempts": attempt,
                        "run_id": res.get("run_id"),
                    }
                return {
                    "status": res.get("status", "completed"),
                    "attempts": attempt,
                    "run_id": res.get("run_id"),
                    "displayed_count": res.get("displayed_count", 0),
                }
            except Exception as exc:
                last_err = mask_sensitive_text(str(exc))
                logger.warning(
                    "定期更新エラー (attempt %d/%d): %s",
                    attempt,
                    bounded_retries + 1,
                    last_err,
                )
                if attempt <= bounded_retries and retry_delay_seconds > 0:
                    time.sleep(retry_delay_seconds)

        return {
            "status": "failed",
            "attempts": bounded_retries + 1,
            "run_id": None,
            "error": last_err,
        }

    if session is not None:
        return _run_with_session(session)

    with session_scope() as scoped_db:
        return _run_with_session(scoped_db)


def run_startup_catchup(
    session: Session | None = None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """ワーカー起動時に停止・スリープ期間の未取得分があれば補完取得を実行する（指示書 §20）。

    Args:
        session (Session | None): DBセッション（省略時は `session_scope()` を使用）。
        now_utc (datetime | None): テスト用現在時刻（UTC）。

    Returns:
        dict[str, Any]: `catchup_executed` (bool) 等を含む辞書。

    Side Effects:
        未取得期間がある場合に `run_catchup_if_needed` を実行する。
    """
    if session is not None:
        return run_catchup_if_needed(session=session, now_utc=now_utc)

    with session_scope() as scoped_db:
        return run_catchup_if_needed(session=scoped_db, now_utc=now_utc)


def create_scheduler(
    blocking: bool = False,
    hour: int | None = None,
    minute: int | None = None,
) -> BlockingScheduler | BackgroundScheduler:
    """毎朝7:00（Asia/Tokyo）に定期更新を実行する APScheduler インスタンスを生成する（指示書 §20）。

    Args:
        blocking (bool): ワーカー単体プロセスとしてブロック実行する場合 True、テスト時は False。
        hour (int | None): 実行時刻（時）。省略時は設定値（7時）。
        minute (int | None): 実行時刻（分）。省略時は設定値（0分）。

    Returns:
        BlockingScheduler | BackgroundScheduler: ジョブ登録済みのスケジューラ。

    Side Effects:
        なし（`start()` は呼び出し側で行う）。

    Examples:
        >>> sched = create_scheduler(blocking=False, hour=7, minute=0)
        >>> len(sched.get_jobs())
        1
    """
    cfg = get_settings()
    sched_hour = cfg.scheduler_hour if hour is None else int(hour)
    sched_min = cfg.scheduler_minute if minute is None else int(minute)

    scheduler_cls = BlockingScheduler if blocking else BackgroundScheduler
    scheduler = scheduler_cls(timezone=JST_ZONE)
    trigger = CronTrigger(hour=sched_hour, minute=sched_min, timezone=JST_ZONE)

    scheduler.add_job(
        run_scheduled_digest_once,
        trigger=trigger,
        id="daily_morning_digest",
        name="毎朝7:00 ニュース収集・選定ジョブ",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=MISFIRE_GRACE_TIME_SECONDS,
    )
    return scheduler


def validate_input(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """入力辞書を検証する。"""
    if payload is None:
        return {"mode": "check"}
    if not isinstance(payload, dict):
        raise TypeError("payload は dict または None である必要があります。")
    return dict(payload)


def execute_logic(validated: dict[str, Any]) -> dict[str, Any]:
    """指定されたモードに応じてスケジューラ構成確認・補完取得・単発定期実行を行う。"""
    mode = str(validated.get("mode", "check"))
    session: Session | None = validated.get("session")

    if mode == "once":
        return run_scheduled_digest_once(
            session=session,
            max_retries=int(validated.get("max_retries", DEFAULT_WORKER_MAX_RETRIES)),
            retry_delay_seconds=float(validated.get("retry_delay_seconds", 0.0)),
        )
    if mode == "catchup":
        return run_startup_catchup(
            session=session,
            now_utc=validated.get("now_utc"),
        )

    sched = create_scheduler(
        blocking=False,
        hour=validated.get("hour"),
        minute=validated.get("minute"),
    )
    jobs = sched.get_jobs()
    return {
        "mode": "check",
        "jobs_count": len(jobs),
        "job_id": jobs[0].id if jobs else None,
        "timezone": str(sched.timezone),
    }


def format_output(result: dict[str, Any]) -> dict[str, Any]:
    """結果辞書を整形して返す。"""
    return dict(result)


def main(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """ワーカー処理または設定確認を実行する。"""
    validated = validate_input(payload)
    res = execute_logic(validated)
    return format_output(res)


if __name__ == "__main__":
    init_all_tables()
    logger.info("定期実行ワーカーを起動しました。停止期間の未取得分補完を確認します...")
    catchup_res = run_startup_catchup()
    logger.info("起動時補完確認結果: %s", catchup_res)
    worker_scheduler = create_scheduler(blocking=True)
    worker_scheduler.start()
