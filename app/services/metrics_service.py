"""Phase 2 のRSS取得量計測・2日分実測判定・レポート集計サービス。

目的（Why）:
    LLM分析を開始する前に、RSSだけで「1日あたり何件取得できるか」「重複はどれくらいか」
    「推定入力トークンはどれくらいか」を実測し、LLMへ送る候補数・分類方法・バッチサイズを
    根拠を持って決められるようにする（指示書 §22 Phase 2、計測期間は2日へ読み替え）。
仕様（What）:
    - `build_daily_metric_data`: RSS収集結果から日次計測値を計算
    - `record_daily_metric`: 日次計測値をDBへ保存（同一日は最新スナップショットで上書き）
    - `run_rss_measurement`: RSS収集のみを実行し、記事保存やLLM呼び出しを行わず計測
    - `get_phase2_report`: 実測日数・暫定/完了状態・カテゴリ別件数・平均文字数・推定トークンを集計
前提条件・依存関係（Assumption / Dependencies）:
    sqlalchemy>=2.0, `app.models.metrics`, `app.repositories.metrics_repo`,
    `app.services.rss_service`, `app.services.dedup_service`
入出力（I/O）:
    入力: SQLAlchemy `Session`、RSS収集結果、重複排除済み記事リスト
    出力: `DailyRssMetric` モデルまたはレポート辞書
注意点（Caution）:
    - 2日分の実測が揃うまで、レポート全体の `is_actual_measurement` は False とする。
    - 日次レコードの `is_actual_measurement` は「実RSS取得に基づくデータか」を表し、
      架空のサンプル値を実測として保存してはならない。
今後の拡張ポイント（Future Work）:
    時間帯別のRSS更新頻度や曜日別の取得量トレンド集計。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 3-1）
"""

from __future__ import annotations

from datetime import date, datetime
from statistics import mean
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.core.config import NEWS_CATEGORIES
from app.core.logging import get_logger
from app.models.metrics import ESTIMATED_TOKENS_PER_CHAR, DailyRssMetric
from app.repositories.metrics_repo import MetricsRepository
from app.repositories.rss_source_repo import RssSourceRepository
from app.repositories.setting_repo import SettingRepository
from app.services.dedup_service import main as run_dedup
from app.services.rss_service import JST_ZONE
from app.services.rss_service import main as collect_rss

# =============================================================================
# 設定値ブロック（Phase 2 計測条件）
# =============================================================================

# 開発指示書の「7日間」を本プロジェクトでは2日間として扱う
PHASE2_REQUIRED_DAYS: int = 2

# レポートに表示する日次データの最大件数
REPORT_METRICS_LIMIT: int = 60

logger = get_logger(__name__)


def current_jst_date() -> date:
    """現在の日本時間（Asia/Tokyo）の日付を返す。

    Returns:
        date: JST の今日の日付。

    Side Effects:
        なし。
    """
    return datetime.now(JST_ZONE).date()


def _safe_mean(values: list[float]) -> float:
    """空リストでも安全に平均値を返す。"""
    if not values:
        return 0.0
    return round(float(mean(values)), 2)


def build_daily_metric_data(
    metric_date: date,
    total_fetched: int,
    deduped_items: list[dict[str, Any]],
    rss_failure_rate: float = 0.0,
    is_actual_measurement: bool = True,
) -> dict[str, Any]:
    """RSS収集結果から1日分の計測値辞書を計算する（純粋関数）。

    解説：
        - 重複率は「取得件数に対して何件が重複で減ったか」で計算する。
        - 文字数・推定トークンは、実際にLLMへ渡す可能性がある「除外されていない記事」を対象にする。
        - 概要が空の記事は平均概要文字数の分母から除外し、0文字で薄めない。
        - トークン数は正確なトークナイザ結果ではなく、日本語・英語混在ニュースの概算係数
          `ESTIMATED_TOKENS_PER_CHAR` を用いた推定値である。

    Args:
        metric_date (date): 計測日（JST）。
        total_fetched (int): RSSから取得した全記事数（除外記事を含む）。
        deduped_items (list[dict[str, Any]]): 重複排除後の記事辞書リスト。
        rss_failure_rate (float): RSS取得元の失敗率（0.0〜1.0）。
        is_actual_measurement (bool): 実RSS取得に基づくデータなら True。

    Returns:
        dict[str, Any]: `MetricsRepository.upsert_daily_metric` へ渡せる計測値辞書。

    Side Effects:
        なし。

    Examples:
        >>> items = [{"title": "AI", "summary_snippet": "概要", "is_excluded": False, "primary_category": "ai_llm"}]
        >>> data = build_daily_metric_data(date(2026, 11, 2), 2, items)
        >>> data["duplicate_rate"]
        0.5
    """
    total = max(0, int(total_fetched))
    deduped_count = max(0, len(deduped_items))

    duplicate_rate = (
        round(max(0.0, (total - deduped_count) / total), 4) if total > 0 else 0.0
    )

    category_counts: dict[str, int] = {key: 0 for key in NEWS_CATEGORIES}
    title_lengths: list[float] = []
    summary_lengths: list[float] = []
    total_prompt_chars = 0

    for item in deduped_items:
        if item.get("is_excluded"):
            continue
        title = str(item.get("title") or "")
        summary = str(item.get("summary_snippet") or "")
        category = str(item.get("primary_category") or "unclassified")
        category_counts[category] = category_counts.get(category, 0) + 1
        title_lengths.append(float(len(title)))
        if summary:
            summary_lengths.append(float(len(summary)))
        total_prompt_chars += len(title) + len(summary)

    return {
        "metric_date": metric_date,
        "is_actual_measurement": bool(is_actual_measurement),
        "total_fetched": total,
        "deduped_count": deduped_count,
        "duplicate_rate": duplicate_rate,
        "category_counts": category_counts,
        "rss_failure_rate": round(min(1.0, max(0.0, float(rss_failure_rate))), 4),
        "avg_title_chars": _safe_mean(title_lengths),
        "avg_summary_chars": _safe_mean(summary_lengths),
        "estimated_input_tokens": int(round(total_prompt_chars * ESTIMATED_TOKENS_PER_CHAR)),
    }


def record_daily_metric(
    session: Session,
    metric_date: date,
    total_fetched: int,
    deduped_items: list[dict[str, Any]],
    rss_failure_rate: float = 0.0,
    is_actual_measurement: bool = True,
) -> DailyRssMetric:
    """1日分のRSS計測値を計算してDBへ保存する。

    Args:
        session (Session): DBセッション。
        metric_date (date): 計測日（JST）。
        total_fetched (int): RSSから取得した全記事数。
        deduped_items (list[dict[str, Any]]): 重複排除後の記事辞書リスト。
        rss_failure_rate (float): RSS取得元の失敗率。
        is_actual_measurement (bool): 実RSS取得に基づくデータなら True。

    Returns:
        DailyRssMetric: 保存後の計測モデル。

    Side Effects:
        `daily_rss_metrics` へ INSERT または UPDATE し commit する。
    """
    metric_data = build_daily_metric_data(
        metric_date=metric_date,
        total_fetched=total_fetched,
        deduped_items=deduped_items,
        rss_failure_rate=rss_failure_rate,
        is_actual_measurement=is_actual_measurement,
    )
    return MetricsRepository(session).upsert_daily_metric(metric_data)


def record_daily_metric_from_collection(
    session: Session,
    collected: dict[str, Any],
    deduped_items: list[dict[str, Any]],
    metric_date: date | None = None,
) -> DailyRssMetric:
    """RSS収集結果辞書から計測値を求め、当日分として保存する。

    Args:
        session (Session): DBセッション。
        collected (dict[str, Any]): `rss_service.main` の戻り値。
        deduped_items (list[dict[str, Any]]): 重複排除後の記事辞書リスト。
        metric_date (date | None): 計測日。省略時は現在のJST日付。

    Returns:
        DailyRssMetric: 保存後の計測モデル。

    Side Effects:
        `daily_rss_metrics` へ INSERT または UPDATE し commit する。
    """
    return record_daily_metric(
        session=session,
        metric_date=metric_date or current_jst_date(),
        total_fetched=len(collected.get("all_fetched_items") or []),
        deduped_items=deduped_items,
        rss_failure_rate=float(collected.get("rss_failure_rate") or 0.0),
        is_actual_measurement=True,
    )


def run_rss_measurement(
    session: Session,
    target_date: date | None = None,
    http_client: httpx.Client | None = None,
    metric_date: date | None = None,
) -> dict[str, Any]:
    """LLMを呼ばずにRSS収集だけを実行し、Phase 2 の日次計測値を記録する。

    解説：
        計測専用の実行ではあるが、ニュース本文の保存は行わない。
        「LLMへ渡す前に、RSSだけでどれだけの候補が集まるか」を測ることが目的である。

    Args:
        session (Session): DBセッション。
        target_date (date | None): 記事の公開日で絞り込む場合の対象日（JST）。
        http_client (httpx.Client | None): テスト用HTTPクライアント。
        metric_date (date | None): 計測日。省略時は現在のJST日付。

    Returns:
        dict[str, Any]: `metric`（本日の計測値）と `report`（Phase 2 集計）を含む辞書。

    Side Effects:
        RSSへのHTTP取得、`daily_rss_metrics` への保存を行う。
    """
    setting = SettingRepository(session).get_or_create()
    src_repo = RssSourceRepository(session)
    src_repo.seed_initial_sources()
    sources = src_repo.list_fetchable_sources()

    collected = collect_rss(
        {
            "sources": sources,
            "summary_char_limit": setting.summary_char_limit,
            "excluded_categories": setting.excluded_categories,
            "target_date": target_date,
            "http_client": http_client,
        }
    )
    deduped = run_dedup(collected["target_items"])
    metric = record_daily_metric_from_collection(
        session=session,
        collected=collected,
        deduped_items=deduped["deduped_items"],
        metric_date=metric_date,
    )
    return {
        "metric": format_daily_metric(metric),
        "report": get_phase2_report(session),
        "source_success_count": int(collected.get("success_sources", 0)),
        "source_failure_count": int(collected.get("failed_sources", 0)),
    }


def format_daily_metric(metric: DailyRssMetric) -> dict[str, Any]:
    """日次計測モデルをJSON/HTML表示用の辞書へ整形する。

    Args:
        metric (DailyRssMetric): 計測モデル。

    Returns:
        dict[str, Any]: 日付をISO文字列にした計測値辞書。

    Side Effects:
        なし。
    """
    return {
        "metric_date": metric.metric_date.isoformat(),
        "is_actual_measurement": metric.is_actual_measurement,
        "total_fetched": metric.total_fetched,
        "deduped_count": metric.deduped_count,
        "duplicate_rate": metric.duplicate_rate,
        "category_counts": dict(metric.category_counts or {}),
        "rss_failure_rate": metric.rss_failure_rate,
        "avg_title_chars": metric.avg_title_chars,
        "avg_summary_chars": metric.avg_summary_chars,
        "estimated_input_tokens": metric.estimated_input_tokens,
        "recorded_at": metric.recorded_at.isoformat() if metric.recorded_at else None,
    }


def get_phase2_report(
    session: Session,
    required_days: int = PHASE2_REQUIRED_DAYS,
) -> dict[str, Any]:
    """Phase 2 の実測進捗と集計レポートを返す。

    Args:
        session (Session): DBセッション。
        required_days (int): 実測が必要な日数（既定2日）。

    Returns:
        dict[str, Any]: 実測日数・完了判定・集計値・日次明細を含む辞書。

    Side Effects:
        なし。
    """
    repo = MetricsRepository(session)
    metrics = repo.list_metrics(limit=REPORT_METRICS_LIMIT, ascending=True)
    actual_metrics = [m for m in metrics if m.is_actual_measurement]
    actual_days = len(actual_metrics)
    is_complete = actual_days >= required_days

    total_fetched = sum(m.total_fetched for m in actual_metrics)
    deduped_count = sum(m.deduped_count for m in actual_metrics)
    aggregate_duplicate_rate = (
        round(max(0.0, (total_fetched - deduped_count) / total_fetched), 4)
        if total_fetched > 0
        else 0.0
    )

    category_counts: dict[str, int] = {key: 0 for key in NEWS_CATEGORIES}
    for metric in actual_metrics:
        for category, count in dict(metric.category_counts or {}).items():
            category_counts[category] = category_counts.get(category, 0) + int(count)

    latest_metric_date = metrics[-1].metric_date.isoformat() if metrics else None
    remaining_days = max(0, required_days - actual_days)

    if not metrics:
        status_label = "未計測"
        measurement_note = (
            "RSS取得量の計測データがまだありません。"
            "「RSS取得量を計測」を実行すると1日分の実測を記録します。"
        )
    elif not is_complete:
        status_label = f"暫定（{actual_days}/{required_days}日）"
        measurement_note = (
            f"実測は{actual_days}日分です。{required_days}日分が揃うまで、"
            "このレポートは暫定値として表示しています。"
        )
    else:
        status_label = f"実測完了（{required_days}日分）"
        measurement_note = (
            f"{required_days}日分のRSS実測が揃いました。"
            "この数値を根拠にLLMへ送る候補数とバッチサイズを決定できます。"
        )

    return {
        "is_actual_measurement": is_complete,
        "status_label": status_label,
        "measurement_note": measurement_note,
        "required_days": int(required_days),
        "actual_days": actual_days,
        "remaining_days": remaining_days,
        "days_recorded": len(metrics),
        "latest_metric_date": latest_metric_date,
        "total_fetched": total_fetched,
        "deduped_count": deduped_count,
        "duplicate_rate": aggregate_duplicate_rate,
        "rss_failure_rate": _safe_mean([m.rss_failure_rate for m in actual_metrics]),
        "avg_title_chars": _safe_mean([m.avg_title_chars for m in actual_metrics]),
        "avg_summary_chars": _safe_mean([m.avg_summary_chars for m in actual_metrics]),
        "estimated_input_tokens": sum(m.estimated_input_tokens for m in actual_metrics),
        "category_counts": category_counts,
        "daily_metrics": [format_daily_metric(m) for m in metrics],
    }


def validate_input(data: dict[str, Any]) -> dict[str, Any]:
    """計測サービス入力辞書を検証する。

    Args:
        data (dict[str, Any]): `session` 等を含む辞書。

    Returns:
        dict[str, Any]: 検証済み辞書。

    Raises:
        ValueError: セッションが不正な場合。

    Side Effects:
        なし。
    """
    if "session" not in data or not isinstance(data["session"], Session):
        raise ValueError("有効な SQLAlchemy Session が必要です。")
    return dict(data)


def execute_logic(validated: dict[str, Any]) -> dict[str, Any]:
    """アクションに応じて計測実行またはレポート集計を行う。"""
    action = str(validated.get("action", "report"))
    session: Session = validated["session"]
    if action == "measure":
        return run_rss_measurement(
            session=session,
            target_date=validated.get("target_date"),
            http_client=validated.get("http_client"),
            metric_date=validated.get("metric_date"),
        )
    return get_phase2_report(
        session=session,
        required_days=int(validated.get("required_days", PHASE2_REQUIRED_DAYS)),
    )


def format_output(result: dict[str, Any]) -> dict[str, Any]:
    """結果辞書をそのまま返す。"""
    return dict(result)


def main(payload: dict[str, Any]) -> dict[str, Any]:
    """計測サービスまたはレポート集計の一連の処理を実行する。"""
    validated = validate_input(payload)
    res = execute_logic(validated)
    return format_output(res)
