"""ニュース記事（news_articles）のSQLAlchemy ORMモデル。

目的（Why）:
    RSSから収集したニュース記事の原情報、正規化URL、重複・関連記事クラスタ識別子、
    一次フィルタ除外状態、および既読・未読状態を永続化する（指示書 §5, §17.2, §19）。
仕様（What）:
    `NewsArticle` テーブル定義とハッシュ生成・整形ヘルパー。
前提条件・依存関係（Assumption / Dependencies）:
    sqlalchemy>=2.0, `app.core.database`, `app.models.rss_source`
入出力（I/O）:
    PostgreSQL / SQLite 上の `news_articles` テーブルレコード。
注意点（Caution）:
    - 同一記事の重複登録を防ぐため `normalized_url` に UNIQUE 制約を付与する。
    - 公開日時（`published_at`）がRSSから取得できない場合、`is_pubdate_verified=False` とし
      対象日内の記事と断定しないこと（指示書 §6.1）。
今後の拡張ポイント（Future Work）:
    将来フェーズでのブックマークフラグや全文検索ベクトルの追加。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 1-4）
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.rss_source import utc_now

# =============================================================================
# 設定値ブロック（ニュース記事モデルの既定値）
# =============================================================================

DEFAULT_ARTICLE_CATEGORY: str = "unclassified"
DEFAULT_IMPORTANCE_SCORE: int = 50


def compute_sha256_hex(value: str) -> str:
    """文字列からSHA-256ハッシュ（16進64文字）を計算する。

    Args:
        value (str): ハッシュ化対象の文字列。

    Returns:
        str: 64文字の16進ハッシュ文字列。

    Side Effects:
        なし。

    Examples:
        >>> len(compute_sha256_hex("sample title"))
        64
    """
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()


class NewsArticle(Base):
    """収集済みニュース記事テーブル（`news_articles`）。"""

    __tablename__ = "news_articles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("rss_sources.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source_name: Mapped[str] = mapped_column(String(150), nullable=False)
    guid: Mapped[str | None] = mapped_column(String(500), nullable=True, index=True)
    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    normalized_url: Mapped[str] = mapped_column(
        String(1000), unique=True, nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    title_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    updated_at_rss: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_pubdate_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rss_categories: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    summary_snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # 別メディアが同じ出来事を報道している場合の関連記事クラスタ識別子（指示書 §5.2）
    cluster_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # 既存記事の更新を検知した場合に True とするフラグ（指示書 §5.2）
    is_updated_article: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_excluded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    exclusion_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    primary_category: Mapped[str] = mapped_column(
        String(50), nullable=False, default=DEFAULT_ARTICLE_CATEGORY, index=True
    )
    importance_score: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_IMPORTANCE_SCORE
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


def validate_input(data: dict[str, Any]) -> dict[str, Any]:
    """記事モデル生成用の入力辞書を検証する。

    Args:
        data (dict[str, Any]): `title`, `url`, `normalized_url`, `source_name` を含む辞書。

    Returns:
        dict[str, Any]: 検証済み辞書。

    Raises:
        ValueError: 必須フィールドが欠落している場合。

    Side Effects:
        なし。

    Examples:
        >>> d = {"title": "AI新技術", "url": "https://example.com/1", "normalized_url": "https://example.com/1", "source_name": "ITmedia"}
        >>> validate_input(d)["title"]
        'AI新技術'
    """
    required = ("title", "url", "normalized_url", "source_name")
    for key in required:
        if not data.get(key):
            raise ValueError(f"NewsArticle 生成には {key} が必須です。")
    return dict(data)


def execute_logic(validated_data: dict[str, Any]) -> NewsArticle:
    """検証済み辞書から `NewsArticle` インスタンスを生成する。

    Args:
        validated_data (dict[str, Any]): `validate_input` 通過後の辞書。

    Returns:
        NewsArticle: ORMモデルインスタンス。

    Side Effects:
        なし。

    Examples:
        >>> d = {"title": "AI新技術", "url": "https://example.com/1", "normalized_url": "https://example.com/1", "source_name": "ITmedia"}
        >>> art = execute_logic(d)
        >>> len(art.title_hash)
        64
    """
    title_str = str(validated_data["title"])
    summary_str = validated_data.get("summary_snippet") or ""
    title_hash = str(validated_data.get("title_hash") or compute_sha256_hex(title_str))
    content_hash = str(
        validated_data.get("content_hash") or compute_sha256_hex(f"{title_str}\n{summary_str}")
    )
    pub_at = validated_data.get("published_at")

    return NewsArticle(
        source_id=validated_data.get("source_id"),
        source_name=str(validated_data["source_name"]),
        guid=validated_data.get("guid"),
        url=str(validated_data["url"]),
        normalized_url=str(validated_data["normalized_url"]),
        title=title_str,
        title_hash=title_hash,
        published_at=pub_at,
        updated_at_rss=validated_data.get("updated_at_rss"),
        is_pubdate_verified=bool(validated_data.get("is_pubdate_verified", pub_at is not None)),
        rss_categories=list(validated_data.get("rss_categories") or []),
        summary_snippet=summary_str if summary_str else None,
        content_hash=content_hash,
        cluster_id=validated_data.get("cluster_id") or title_hash[:16],
        is_updated_article=bool(validated_data.get("is_updated_article", False)),
        is_read=bool(validated_data.get("is_read", False)),
        is_excluded=bool(validated_data.get("is_excluded", False)),
        exclusion_reason=validated_data.get("exclusion_reason"),
        primary_category=str(validated_data.get("primary_category", DEFAULT_ARTICLE_CATEGORY)),
        importance_score=int(validated_data.get("importance_score", DEFAULT_IMPORTANCE_SCORE)),
    )


def format_output(article: NewsArticle) -> dict[str, Any]:
    """`NewsArticle` インスタンスを辞書に整形する（トークン量や料金は含めない：指示書 §17.2）。

    Args:
        article (NewsArticle): 記事モデルインスタンス。

    Returns:
        dict[str, Any]: ニュースカード等へ渡す記事単位の辞書。

    Side Effects:
        なし。

    Examples:
        >>> d = {"title": "AI新技術", "url": "https://example.com/1", "normalized_url": "https://example.com/1", "source_name": "ITmedia"}
        >>> out = format_output(execute_logic(d))
        >>> "input_tokens" in out
        False
    """
    return {
        "id": article.id,
        "source_id": article.source_id,
        "source_name": article.source_name,
        "title": article.title,
        "url": article.url,
        "normalized_url": article.normalized_url,
        "published_at": article.published_at,
        "updated_at_rss": article.updated_at_rss,
        "is_pubdate_verified": article.is_pubdate_verified,
        "primary_category": article.primary_category,
        "importance_score": article.importance_score,
        "summary_snippet": article.summary_snippet,
        "cluster_id": article.cluster_id,
        "is_updated_article": article.is_updated_article,
        "is_read": article.is_read,
        "is_excluded": article.is_excluded,
        "exclusion_reason": article.exclusion_reason,
    }


def main(data: dict[str, Any]) -> dict[str, Any]:
    """入力検証・記事モデル生成・辞書整形の一連の流れを実行する。

    Args:
        data (dict[str, Any]): 記事入力データ。

    Returns:
        dict[str, Any]: 整形済み記事辞書。

    Side Effects:
        なし。

    Examples:
        >>> d = {"title": "AI新技術", "url": "https://example.com/1", "normalized_url": "https://example.com/1", "source_name": "ITmedia"}
        >>> main(d)["source_name"]
        'ITmedia'
    """
    validated = validate_input(data)
    article = execute_logic(validated)
    return format_output(article)
