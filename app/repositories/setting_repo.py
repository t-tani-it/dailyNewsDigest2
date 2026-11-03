"""ユーザー設定（user_settings）および二重実行防止ロックのRepositoryモジュール。

目的（Why）:
    初回承認状態、選択カテゴリ、表示件数、LLM設定の永続化と、
    定期更新・手動更新の二重実行防止ロック管理をDBトランザクションで行う（指示書 §6.3, §17.6, §20）。
仕様（What）:
    `SettingRepository` クラスによるシングルトン設定取得・更新・排他ロック制御。
前提条件・依存関係（Assumption / Dependencies）:
    sqlalchemy>=2.0, `app.core.config`, `app.models.setting`
入出力（I/O）:
    入力: SQLAlchemy `Session`、設定更新辞書
    出力: `UserSetting` インスタンスまたはロック取得成否 `bool`
注意点（Caution）:
    ロック獲得中にプロセスが異常終了した場合でも永久デッドロックにならないよう、
    `stale_minutes`（既定30分）を超えた古いロックは期限切れとして再獲得を許可する。
今後の拡張ポイント（Future Work）:
    PostgreSQL アドバイザリロック（`pg_try_advisory_lock`）との併用。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 2-1）
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import (
    DEFAULT_DISPLAY_COUNT,
    DEFAULT_EXCLUDED_CATEGORIES,
    DEFAULT_SELECTED_CATEGORIES,
    DEFAULT_SUMMARY_CHAR_LIMIT,
    get_settings,
)
from app.models.rss_source import utc_now
from app.models.setting import UserSetting
from app.models.setting import validate_input as validate_setting_dict

# =============================================================================
# 設定値ブロック
# =============================================================================

DEFAULT_LOCK_STALE_MINUTES: int = 30


def _ensure_utc(dt: datetime | None) -> datetime | None:
    """SQLite等からタイムゾーン無しで返された `datetime` をUTCとして正規化する。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class SettingRepository:
    """`user_settings` テーブル（id=1）を管理するRepositoryクラス。"""

    def __init__(self, session: Session) -> None:
        """Repositoryを初期化する。"""
        self.session = session

    def get_or_create(self) -> UserSetting:
        """シングルトンのユーザー設定レコード（id=1）を取得、未作成なら初期作成する。

        Returns:
            UserSetting: 現在のユーザー設定オブジェクト。

        Side Effects:
            初回呼び出し時に `user_settings` (id=1) を INSERT し commit する。
        """
        setting = self.session.get(UserSetting, 1)
        if setting is None:
            env_cfg = get_settings()
            setting = UserSetting(
                id=1,
                is_initial_approved=False,
                selected_categories=list(DEFAULT_SELECTED_CATEGORIES),
                excluded_categories=list(DEFAULT_EXCLUDED_CATEGORIES),
                display_count=env_cfg.default_display_count or DEFAULT_DISPLAY_COUNT,
                sort_order="importance",
                target_days=1,
                auto_update_enabled=False,
                auto_update_time=f"{env_cfg.scheduler_hour:02d}:{env_cfg.scheduler_minute:02d}",
                summary_char_limit=env_cfg.rss_summary_char_limit or DEFAULT_SUMMARY_CHAR_LIMIT,
                llm_enabled=env_cfg.llm_enabled,
                selection_mode="auto",
                preferred_model_id=None,
                auto_switch_enabled=env_cfg.llm_auto_switch,
                daily_api_call_limit=env_cfg.llm_daily_call_limit,
            )
            self.session.add(setting)
            self.session.commit()
            self.session.refresh(setting)
        return setting

    def update_settings(self, updates: dict[str, Any]) -> UserSetting:
        """検証済みの設定変更辞書をユーザー設定へ反映する。

        Args:
            updates (dict[str, Any]): 更新対象フィールドの辞書。

        Returns:
            UserSetting: 更新後の設定オブジェクト。

        Side Effects:
            `user_settings` を UPDATE し commit する。
        """
        validated = validate_setting_dict(updates)
        setting = self.get_or_create()

        allowed_fields = {
            "is_initial_approved",
            "selected_categories",
            "excluded_categories",
            "display_count",
            "sort_order",
            "target_days",
            "auto_update_enabled",
            "auto_update_time",
            "summary_char_limit",
            "llm_enabled",
            "selection_mode",
            "preferred_model_id",
            "auto_switch_enabled",
            "daily_api_call_limit",
        }
        for key, value in validated.items():
            if key in allowed_fields:
                setattr(setting, key, value)

        setting.updated_at = utc_now()
        self.session.commit()
        self.session.refresh(setting)
        return setting

    def approve_initial_onboarding(
        self, selected_categories: list[str]
    ) -> UserSetting:
        """初回起動確認で「はい」が選択された際に承認状態と毎朝の自動更新を有効化する（指示書 §6.3）。

        Args:
            selected_categories (list[str]): ユーザーが選択したカテゴリリスト。

        Returns:
            UserSetting: 承認済みのユーザー設定。

        Side Effects:
            `is_initial_approved=True`, `auto_update_enabled=True` を保存し commit する。
        """
        return self.update_settings(
            {
                "is_initial_approved": True,
                "auto_update_enabled": True,
                "selected_categories": list(selected_categories),
            }
        )

    def acquire_run_lock(
        self, run_id: str, stale_minutes: int = DEFAULT_LOCK_STALE_MINUTES
    ) -> bool:
        """二重実行防止用の排他ロックを獲得する（指示書 §20）。

        Args:
            run_id (str): ロックを要求する `run_id`。
            stale_minutes (int): 古いロックを期限切れとみなす分数。

        Returns:
            bool: ロック獲得成功時 True、他プロセス実行中で獲得不可なら False。

        Side Effects:
            獲得成功時に `active_run_lock_id` と `active_run_locked_at` を更新し commit する。
        """
        setting = self.get_or_create()
        now = utc_now()
        locked_at = _ensure_utc(setting.active_run_locked_at)

        if (
            setting.active_run_lock_id
            and setting.active_run_lock_id != run_id
            and locked_at is not None
            and (now - locked_at) < timedelta(minutes=stale_minutes)
        ):
            return False

        setting.active_run_lock_id = run_id
        setting.active_run_locked_at = now
        setting.updated_at = now
        self.session.commit()
        return True

    def release_run_lock(
        self,
        run_id: str | None = None,
        mark_success: bool = False,
    ) -> UserSetting:
        """排他ロックを解放し、成功時は `last_successful_run_at` を更新する（指示書 §20）。

        Args:
            run_id (str | None): 解放対象の `run_id`（None の場合は強制解放）。
            mark_success (bool): True の場合、`last_successful_run_at` に現在時刻を記録する。

        Returns:
            UserSetting: 更新後の設定オブジェクト。

        Side Effects:
            ロック列をクリアし commit する。
        """
        setting = self.get_or_create()
        now = utc_now()
        if run_id is None or setting.active_run_lock_id == run_id:
            setting.active_run_lock_id = None
            setting.active_run_locked_at = None
        if mark_success:
            setting.last_successful_run_at = now
        setting.updated_at = now
        self.session.commit()
        self.session.refresh(setting)
        return setting


def validate_input(data: dict[str, Any]) -> dict[str, Any]:
    """入力辞書を検証する。"""
    if "session" not in data or not isinstance(data["session"], Session):
        raise ValueError("有効な SQLAlchemy Session が必要です。")
    return data


def execute_logic(validated: dict[str, Any]) -> UserSetting:
    """設定を取得（必要なら更新）する。"""
    repo = SettingRepository(validated["session"])
    if validated.get("updates"):
        return repo.update_settings(validated["updates"])
    return repo.get_or_create()


def format_output(setting: UserSetting) -> dict[str, Any]:
    """設定モデルを辞書へ整形する。"""
    return {
        "is_initial_approved": setting.is_initial_approved,
        "selected_categories": setting.selected_categories,
        "display_count": setting.display_count,
        "sort_order": setting.sort_order,
        "auto_update_enabled": setting.auto_update_enabled,
        "llm_enabled": setting.llm_enabled,
    }


def main(session: Session, updates: dict[str, Any] | None = None) -> dict[str, Any]:
    """設定の取得・更新・整形を一括実行する。"""
    validated = validate_input({"session": session, "updates": updates})
    setting = execute_logic(validated)
    return format_output(setting)
