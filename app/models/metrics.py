"""Phase 2 用の日次RSS取得量計測データ（daily_rss_metrics）のSQLAlchemy ORMモデル。

目的（Why）:
    RSS収集のみを用いて2日間（開発指示書の7日間を読み替え）の実測状況を記録し、
    LLMへ送る候補記事数・分類方法・バッチサイズを最適化する根拠データを保存する（指示書 §22 Phase 2）。
仕様（What）:
    `DailyRssMetric` テーブル定義（日別記事数、カテゴリ別件数、重複率、失敗率、
    平均文字数、推定入力トークン量、実測データフラグ）。
前提条件・依存関係（Assumption / Dependencies）:
    sqlalchemy>=2.0, `app.core.database`, `app.models.rss_source`
入出力（I/O）:
    PostgreSQL / SQLite 上の `daily_rss_metrics` テーブルレコード。
注意点（Caution）:
    実際の2日間が経過していない場合、架空のデータを `is_actual_measurement=True` として
    登録してはならない（指示書 §22 Phase 2）。
今後の拡張ポイント（Future Work）:
    時間帯別（朝・昼・夜）のRSS更新頻度ヒストグラム保持。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 1-4）
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.rss_source import utc_now

# =============================================================================
# 設定値ブロック（推定トークン計算係数）
# =============================================================================

# 日本語・英語混在ニュースにおける1文字あたりの概算トークン係数
ESTIMATED_TOKENS_PER_CHAR: float = 0.85


class DailyRssMetric(Base):
    """日次RSS取得量計測テーブル（`daily_rss_metrics`）。"""

    __tablename__ = "daily_rss_metrics"

    metric_date: Mapped[date] = mapped_column(Date, primary_key=True)
    # 実測データか仮データ（暫定）かを明確に区別するフラグ（指示書 §22 Phase 2）
    is_actual_measurement: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    total_fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deduped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicate_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    category_counts: Mapped[dict[str, int]] = mapped_column(JSON, nullable=False, default=dict)
    rss_failure_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    avg_title_chars: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    avg_summary_chars: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    estimated_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


def validate_input(data: dict[str, Any]) -> dict[str, Any]:
    """日次RSS計測データ生成用の入力辞書を検証する。

    Args:
        data (dict[str, Any]): `metric_date` 等を含む辞書。

    Returns:
        dict[str, Any]: 検証済み辞書。

    Raises:
        ValueError: `metric_date` が指定されていない場合。

    Side Effects:
        なし。

    Examples:
        >>> from datetime import date
        >>> validate_input({"metric_date": date(2026, 11, 2)})["metric_date"]
        datetime.date(2026, 11, 2)
    """
    if not data.get("metric_date"):
        raise ValueError("DailyRssMetric 生成には metric_date が必須です。")
    return dict(data)


def execute_logic(validated_data: dict[str, Any]) -> DailyRssMetric:
    """検証済み辞書から `DailyRssMetric` インスタンスを生成する。

    Args:
        validated_data (dict[str, Any]): `validate_input` 通過後の辞書。

    Returns:
        DailyRssMetric: ORMモデルインスタンス。

    Side Effects:
        なし。

    Examples:
        >>> from datetime import date
        >>> m = execute_logic({"metric_date": date(2026, 11, 2), "total_fetched": 100, "deduped_count": 80})
        >>> m.duplicate_rate
        0.2
    """
    total = int(validated_data.get("total_fetched", 0))
    deduped = int(validated_data.get("deduped_count", 0))
    if "duplicate_rate" in validated_data:
        dup_rate = float(validated_data["duplicate_rate"])
    elif total > 0:
        dup_rate = round(max(0.0, (total - deduped) / total), 4)
    else:
        dup_rate = 0.0

    return DailyRssMetric(
        metric_date=validated_data["metric_date"],
        is_actual_measurement=bool(validated_data.get("is_actual_measurement", True)),
        total_fetched=total,
        deduped_count=deduped,
        duplicate_rate=dup_rate,
        category_counts=dict(validated_data.get("category_counts") or {}),
        rss_failure_rate=float(validated_data.get("rss_failure_rate", 0.0)),
        avg_title_chars=float(validated_data.get("avg_title_chars", 0.0)),
        avg_summary_chars=float(validated_data.get("avg_summary_chars", 0.0)),
        estimated_input_tokens=int(validated_data.get("estimated_input_tokens", 0)),
    )


def format_output(metric: DailyRssMetric) -> dict[str, Any]:
    """`DailyRssMetric` インスタンスを辞書へ整形する。

    Args:
        metric (DailyRssMetric): 計測データORMインスタンス。

    Returns:
        dict[str, Any]: 整形済み辞書。

    Side Effects:
        なし。

    Examples:
        >>> from datetime import date
        >>> format_output(execute_logic({"metric_date": date(2026, 11, 2)}))["is_actual_measurement"]
        True
    """
    return {
        "metric_date": metric.metric_date,
        "is_actual_measurement": metric.is_actual_measurement,
        "total_fetched": metric.total_fetched,
        "deduped_count": metric.deduped_count,
        "duplicate_rate": metric.duplicate_rate,
        "category_counts": metric.category_counts,
        "rss_failure_rate": metric.rss_failure_rate,
        "avg_title_chars": metric.avg_title_chars,
        "avg_summary_chars": metric.avg_summary_chars,
        "estimated_input_tokens": metric.estimated_input_tokens,
    }


def main(data: dict[str, Any]) -> dict[str, Any]:
    """入力検証・計測モデル生成・辞書整形の一連の処理を実行する。

    Args:
        data (dict[str, Any]): 計測データ辞書。

    Returns:
        dict[str, Any]: 整形済み辞書。

    Side Effects:
        なし。

    Examples:
        >>> from datetime import date
        >>> main({"metric_date": date(2026, 11, 2), "total_fetched": 50, "deduped_count": 40})["duplicate_rate"]
        0.2
    """
    validated = validate_input(data)
    instance = execute_logic(validated)
    return format_output(instance)
