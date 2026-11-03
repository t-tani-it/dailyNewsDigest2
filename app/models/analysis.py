"""AI分析結果（news_analyses）のSQLAlchemy ORMモデル。

目的（Why）:
    ニュース記事に対するカテゴリ分類、重要度、AIによる日本語要約、
    および「なぜ重要なのか」の説明を保存し、カテゴリ変更時の再利用を可能にする（指示書 §7.1, §8, §19）。
仕様（What）:
    `NewsAnalysis` テーブル定義と「概要情報不足」ステータス管理。
前提条件・依存関係（Assumption / Dependencies）:
    sqlalchemy>=2.0, `app.core.database`, `app.models.rss_source`
入出力（I/O）:
    PostgreSQL / SQLite 上の `news_analyses` テーブルレコード。
注意点（Caution）:
    タイトル・概要だけでは正確に要約できない場合、推測で補完せず
    `summary_status="insufficient_info"` かつ `summary_ja="概要情報不足"` として保存すること（指示書 §7.1）。
今後の拡張ポイント（Future Work）:
    複数モデル間の要約品質比較用メタデータの保持。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 1-4）
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.rss_source import utc_now

# =============================================================================
# 設定値ブロック（AI分析ステータスおよび情報不足時ラベル）
# =============================================================================

SUMMARY_STATUS_COMPLETED: str = "completed"
SUMMARY_STATUS_INSUFFICIENT: str = "insufficient_info"
SUMMARY_STATUS_SKIPPED: str = "skipped"
SUMMARY_STATUS_FAILED: str = "failed"
INSUFFICIENT_INFO_TEXT: str = "概要情報不足"


class NewsAnalysis(Base):
    """AI分析結果テーブル（`news_analyses`）。"""

    __tablename__ = "news_analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    article_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("news_articles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    run_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("news_runs.run_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model_id: Mapped[str] = mapped_column(String(150), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    is_excluded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    importance_score: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    summary_ja: Mapped[str] = mapped_column(Text, nullable=False)
    why_important: Mapped[str] = mapped_column(Text, nullable=False)
    summary_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=SUMMARY_STATUS_COMPLETED
    )
    analyzed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


def validate_input(data: dict[str, Any]) -> dict[str, Any]:
    """AI分析結果生成用の入力辞書を検証する。

    Args:
        data (dict[str, Any]): `article_id`, `provider`, `model_id`, `category` を含む辞書。

    Returns:
        dict[str, Any]: 検証済み辞書。

    Raises:
        ValueError: 必須項目が不足している場合。

    Side Effects:
        なし。

    Examples:
        >>> d = {"article_id": 1, "provider": "gemini", "model_id": "gemini-2.0-flash", "category": "ai_llm"}
        >>> validate_input(d)["provider"]
        'gemini'
    """
    required = ("article_id", "provider", "model_id", "category")
    for key in required:
        if data.get(key) is None or data.get(key) == "":
            raise ValueError(f"NewsAnalysis 生成には {key} が必須です。")
    return dict(data)


def execute_logic(validated_data: dict[str, Any]) -> NewsAnalysis:
    """検証済み辞書から `NewsAnalysis` インスタンスを生成する。

    Args:
        validated_data (dict[str, Any]): `validate_input` 通過後の辞書。

    Returns:
        NewsAnalysis: ORMモデルインスタンス。

    Side Effects:
        なし。

    Examples:
        >>> d = {"article_id": 1, "provider": "gemini", "model_id": "gemini-2.0-flash", "category": "ai_llm"}
        >>> obj = execute_logic(d)
        >>> obj.summary_ja
        '概要情報不足'
    """
    status = str(validated_data.get("summary_status", SUMMARY_STATUS_COMPLETED))
    summary = str(validated_data.get("summary_ja") or "").strip()
    why = str(validated_data.get("why_important") or "").strip()

    # タイトル・概要だけで正確に要約できない場合は推測補完せず「概要情報不足」とする（指示書 §7.1）
    if not summary or status == SUMMARY_STATUS_INSUFFICIENT:
        summary = INSUFFICIENT_INFO_TEXT
        status = SUMMARY_STATUS_INSUFFICIENT
    if not why:
        why = INSUFFICIENT_INFO_TEXT

    return NewsAnalysis(
        article_id=int(validated_data["article_id"]),
        run_id=validated_data.get("run_id"),
        provider=str(validated_data["provider"]),
        model_id=str(validated_data["model_id"]),
        category=str(validated_data["category"]),
        is_excluded=bool(validated_data.get("is_excluded", False)),
        importance_score=int(validated_data.get("importance_score", 50)),
        summary_ja=summary,
        why_important=why,
        summary_status=status,
    )


def format_output(analysis: NewsAnalysis) -> dict[str, Any]:
    """`NewsAnalysis` インスタンスを辞書へ整形する。

    Args:
        analysis (NewsAnalysis): 分析結果ORMインスタンス。

    Returns:
        dict[str, Any]: 整形済み辞書。

    Side Effects:
        なし。

    Examples:
        >>> d = {"article_id": 1, "provider": "gemini", "model_id": "gemini-2.0-flash", "category": "ai_llm", "summary_ja": "要約", "why_important": "理由"}
        >>> format_output(execute_logic(d))["summary_status"]
        'completed'
    """
    return {
        "id": analysis.id,
        "article_id": analysis.article_id,
        "run_id": analysis.run_id,
        "provider": analysis.provider,
        "model_id": analysis.model_id,
        "category": analysis.category,
        "is_excluded": analysis.is_excluded,
        "importance_score": analysis.importance_score,
        "summary_ja": analysis.summary_ja,
        "why_important": analysis.why_important,
        "summary_status": analysis.summary_status,
        "analyzed_at": analysis.analyzed_at,
    }


def main(data: dict[str, Any]) -> dict[str, Any]:
    """入力検証・分析モデル生成・辞書整形の一連の処理を実行する。

    Args:
        data (dict[str, Any]): 分析結果データ辞書。

    Returns:
        dict[str, Any]: 整形済み辞書。

    Side Effects:
        なし。

    Examples:
        >>> d = {"article_id": 1, "provider": "gemini", "model_id": "gemini-2.0-flash", "category": "ai_llm"}
        >>> main(d)["summary_ja"]
        '概要情報不足'
    """
    validated = validate_input(data)
    instance = execute_logic(validated)
    return format_output(instance)
