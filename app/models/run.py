"""ニュース処理実行履歴（news_runs）および実行別記事一覧（run_articles）モデル。

目的（Why）:
    ニュース処理の開始から終了までを一意の `run_id`（1実行単位）として管理し、
    その実行時点で選定された記事順位と分析スナップショットを不変に保存する（指示書 §14, §18, §19）。
仕様（What）:
    - `NewsRun`: 1実行の実行方法・件数内訳・実行状態・親実行参照の管理
    - `RunArticle`: 実行ごとの表示順位（1〜15位）と表示時点の分析結果参照の保持
前提条件・依存関係（Assumption / Dependencies）:
    sqlalchemy>=2.0, `app.core.database`, `app.models.rss_source`
入出力（I/O）:
    PostgreSQL / SQLite 上の `news_runs` および `run_articles` テーブルレコード。
注意点（Caution）:
    後日のニュース更新やカテゴリ変更で過去の実行結果が書き換わらないよう、
    `RunArticle` には `analysis_id` に加えて表示時点のスナップショット値も保持する。
今後の拡張ポイント（Future Work）:
    実行ステップ別の所要時間（ミリ秒）プロファイリング列の追加。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 1-4）
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.rss_source import utc_now

# =============================================================================
# 設定値ブロック（実行種別および実行状態定数）
# =============================================================================

RUN_TYPE_INITIAL: str = "initial"
RUN_TYPE_MANUAL: str = "manual"
RUN_TYPE_SCHEDULED: str = "scheduled"
RUN_TYPE_RECATEGORIZE: str = "recategorize"
RUN_TYPE_RETRY: str = "retry"
RUN_TYPE_CATCHUP: str = "catchup"

VALID_RUN_TYPES: tuple[str, ...] = (
    RUN_TYPE_INITIAL,
    RUN_TYPE_MANUAL,
    RUN_TYPE_SCHEDULED,
    RUN_TYPE_RECATEGORIZE,
    RUN_TYPE_RETRY,
    RUN_TYPE_CATCHUP,
)

RUN_STATUS_RUNNING: str = "running"
RUN_STATUS_COMPLETED: str = "completed"
RUN_STATUS_AI_PENDING: str = "ai_pending"
RUN_STATUS_FAILED: str = "failed"
RUN_STATUS_PREVIEW: str = "preview"


def generate_run_id() -> str:
    """一意の `run_id`（UUID4文字列）を生成する。

    Returns:
        str: 36文字のUUID4文字列。

    Side Effects:
        なし。

    Examples:
        >>> len(generate_run_id())
        36
    """
    return str(uuid.uuid4())


class NewsRun(Base):
    """ニュース処理実行履歴テーブル（`news_runs`）。"""

    __tablename__ = "news_runs"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_run_id)
    parent_run_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("news_runs.run_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    run_type: Mapped[str] = mapped_column(String(30), nullable=False, default=RUN_TYPE_MANUAL)
    target_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    selected_categories: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fetched_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deduped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ai_classified_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ai_summarized_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    displayed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default=RUN_STATUS_RUNNING)
    reused_analysis: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    used_provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    used_model_id: Mapped[str | None] = mapped_column(String(150), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class RunArticle(Base):
    """実行別記事一覧テーブル（`run_articles`）。"""

    __tablename__ = "run_articles"
    __table_args__ = (UniqueConstraint("run_id", "article_id", name="uq_run_article"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("news_runs.run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    article_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("news_articles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    analysis_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("news_analyses.id", ondelete="SET NULL"),
        nullable=True,
    )
    display_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    # 過去実行の表示結果が後日の再分析や記事更新で書き換わらないためのスナップショット列（指示書 §18）
    snapshot_category: Mapped[str] = mapped_column(
        String(50), nullable=False, default="unclassified"
    )
    snapshot_importance: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    snapshot_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot_why_important: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_reused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    selected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


def validate_input(data: dict[str, Any]) -> dict[str, Any]:
    """`NewsRun` 生成用の入力辞書を検証する。

    Args:
        data (dict[str, Any]): `run_type` 等を含む辞書。

    Returns:
        dict[str, Any]: 検証済み辞書。

    Raises:
        ValueError: `run_type` が未定義の値である場合。

    Side Effects:
        なし。

    Examples:
        >>> validate_input({"run_type": "manual"})["run_type"]
        'manual'
    """
    run_type = str(data.get("run_type", RUN_TYPE_MANUAL))
    if run_type not in VALID_RUN_TYPES:
        raise ValueError(f"未対応の run_type です: {run_type}")
    return dict(data)


def execute_logic(validated_data: dict[str, Any]) -> NewsRun:
    """検証済み辞書から `NewsRun` インスタンスを生成する。

    Args:
        validated_data (dict[str, Any]): `validate_input` 通過後の辞書。

    Returns:
        NewsRun: ORMモデルインスタンス。

    Side Effects:
        なし。

    Examples:
        >>> r = execute_logic({"run_type": "scheduled"})
        >>> r.run_type
        'scheduled'
    """
    return NewsRun(
        run_id=str(validated_data.get("run_id") or generate_run_id()),
        parent_run_id=validated_data.get("parent_run_id"),
        run_type=str(validated_data.get("run_type", RUN_TYPE_MANUAL)),
        target_date=validated_data.get("target_date"),
        selected_categories=list(validated_data.get("selected_categories") or []),
        fetched_count=int(validated_data.get("fetched_count", 0)),
        deduped_count=int(validated_data.get("deduped_count", 0)),
        ai_classified_count=int(validated_data.get("ai_classified_count", 0)),
        ai_summarized_count=int(validated_data.get("ai_summarized_count", 0)),
        displayed_count=int(validated_data.get("displayed_count", 0)),
        status=str(validated_data.get("status", RUN_STATUS_RUNNING)),
        reused_analysis=bool(validated_data.get("reused_analysis", False)),
        used_provider=validated_data.get("used_provider"),
        used_model_id=validated_data.get("used_model_id"),
        error_message=validated_data.get("error_message"),
    )


def format_output(run_obj: NewsRun) -> dict[str, Any]:
    """`NewsRun` インスタンスをサマリー表示用辞書へ整形する。

    Args:
        run_obj (NewsRun): 実行履歴ORMインスタンス。

    Returns:
        dict[str, Any]: 整形済み辞書。

    Side Effects:
        なし。

    Examples:
        >>> out = format_output(execute_logic({"run_type": "manual"}))
        >>> out["status"]
        'running'
    """
    return {
        "run_id": run_obj.run_id,
        "parent_run_id": run_obj.parent_run_id,
        "run_type": run_obj.run_type,
        "target_date": run_obj.target_date,
        "selected_categories": run_obj.selected_categories,
        "started_at": run_obj.started_at,
        "finished_at": run_obj.finished_at,
        "fetched_count": run_obj.fetched_count,
        "deduped_count": run_obj.deduped_count,
        "ai_classified_count": run_obj.ai_classified_count,
        "ai_summarized_count": run_obj.ai_summarized_count,
        "displayed_count": run_obj.displayed_count,
        "status": run_obj.status,
        "reused_analysis": run_obj.reused_analysis,
        "used_provider": run_obj.used_provider,
        "used_model_id": run_obj.used_model_id,
        "error_message": run_obj.error_message,
    }


def main(data: dict[str, Any]) -> dict[str, Any]:
    """入力検証・実行履歴モデル生成・辞書整形の一連の処理を実行する。

    Args:
        data (dict[str, Any]): 実行履歴データ。

    Returns:
        dict[str, Any]: 整形済み辞書。

    Side Effects:
        なし。

    Examples:
        >>> main({"run_type": "manual"})["run_type"]
        'manual'
    """
    validated = validate_input(data)
    instance = execute_logic(validated)
    return format_output(instance)
