"""RSS取得元（rss_sources）のSQLAlchemy ORMモデル。

目的（Why）:
    公式RSSフィードのURL、有効・無効、利用条件確認状態、保存・AI要約可否、
    および取得エラー履歴を永続化し、未確認ソースの自動利用を防止する（指示書 §5.3, §19）。
仕様（What）:
    `RssSource` テーブル定義と利用可否判定ヘルパー。
前提条件・依存関係（Assumption / Dependencies）:
    sqlalchemy>=2.0, `app.core.database`
入出力（I/O）:
    PostgreSQL / SQLite 上の `rss_sources` テーブルレコード。
注意点（Caution）:
    `terms_status == "verified"` かつ `is_enabled == True` の情報源のみ自動収集に使用すること。
今後の拡張ポイント（Future Work）:
    HTTP ETag / Last-Modified ヘッダー保存カラムの追加。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 1-4）
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# =============================================================================
# 設定値ブロック（利用条件ステータス定数）
# =============================================================================

TERMS_STATUS_VERIFIED: str = "verified"
TERMS_STATUS_UNVERIFIED: str = "unverified"
TERMS_STATUS_RESTRICTED: str = "restricted"
VALID_TERMS_STATUSES: tuple[str, ...] = (
    TERMS_STATUS_VERIFIED,
    TERMS_STATUS_UNVERIFIED,
    TERMS_STATUS_RESTRICTED,
)


def utc_now() -> datetime:
    """現在時刻をUTCタイムゾーン付き `datetime` として返す。

    Returns:
        datetime: UTCタイムゾーン付きの現在日時。

    Side Effects:
        なし。

    Examples:
        >>> utc_now().tzinfo is not None
        True
    """
    return datetime.now(UTC)


class RssSource(Base):
    """RSS取得元管理テーブル（`rss_sources`）。"""

    __tablename__ = "rss_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    url: Mapped[str] = mapped_column(String(500), unique=True, nullable=False, index=True)
    category_hint: Mapped[str] = mapped_column(String(50), nullable=False, default="unclassified")
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    terms_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=TERMS_STATUS_UNVERIFIED
    )
    allow_store_summary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    allow_ai_summary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str] = mapped_column(String(50), nullable=False, default="never")
    error_history: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

    @property
    def is_safe_to_fetch(self) -> bool:
        """自動収集してよい情報源かどうかを判定する（指示書 §5.3）。

        Returns:
            bool: 有効かつ利用条件が確認済み（`verified`）の場合のみ True。

        Side Effects:
            なし。

        Examples:
            >>> src = RssSource(name="Test", url="https://example.com/rss", is_enabled=True, terms_status="verified")
            >>> src.is_safe_to_fetch
            True
        """
        return bool(self.is_enabled and self.terms_status == TERMS_STATUS_VERIFIED)


def validate_input(data: dict[str, Any]) -> dict[str, Any]:
    """RSS取得元モデル生成用の入力辞書を検証する。

    Args:
        data (dict[str, Any]): `name`, `url` 等を含む辞書。

    Returns:
        dict[str, Any]: 検証済み辞書。

    Raises:
        ValueError: 必須項目欠落または `terms_status` 不正時。

    Side Effects:
        なし。

    Examples:
        >>> validate_input({"name": "NHK", "url": "https://www.nhk.or.jp/rss/news/cat0.xml"})["name"]
        'NHK'
    """
    if not data.get("name") or not data.get("url"):
        raise ValueError("RSS取得元には name と url が必須です。")
    terms = str(data.get("terms_status", TERMS_STATUS_UNVERIFIED))
    if terms not in VALID_TERMS_STATUSES:
        raise ValueError(f"不正な terms_status です: {terms}")
    return dict(data)


def execute_logic(validated_data: dict[str, Any]) -> RssSource:
    """検証済み辞書から `RssSource` インスタンスを生成する。

    Args:
        validated_data (dict[str, Any]): `validate_input` 通過後の辞書。

    Returns:
        RssSource: ORMモデルインスタンス。

    Side Effects:
        なし（DB未保存）。

    Examples:
        >>> obj = execute_logic({"name": "NHK", "url": "https://www.nhk.or.jp/rss/news/cat0.xml"})
        >>> obj.is_safe_to_fetch
        False
    """
    return RssSource(
        name=str(validated_data["name"]),
        url=str(validated_data["url"]),
        category_hint=str(validated_data.get("category_hint", "unclassified")),
        is_enabled=bool(validated_data.get("is_enabled", True)),
        terms_status=str(validated_data.get("terms_status", TERMS_STATUS_UNVERIFIED)),
        allow_store_summary=bool(validated_data.get("allow_store_summary", False)),
        allow_ai_summary=bool(validated_data.get("allow_ai_summary", False)),
    )


def format_output(source: RssSource) -> dict[str, Any]:
    """`RssSource` インスタンスを辞書形式に整形する。

    Args:
        source (RssSource): ORMモデルインスタンス。

    Returns:
        dict[str, Any]: 表示・シリアライズ用辞書。

    Side Effects:
        なし。

    Examples:
        >>> s = execute_logic({"name": "NHK", "url": "https://www.nhk.or.jp/rss/news/cat0.xml", "terms_status": "verified"})
        >>> format_output(s)["is_safe_to_fetch"]
        True
    """
    return {
        "id": source.id,
        "name": source.name,
        "url": source.url,
        "category_hint": source.category_hint,
        "is_enabled": source.is_enabled,
        "terms_status": source.terms_status,
        "allow_store_summary": source.allow_store_summary,
        "allow_ai_summary": source.allow_ai_summary,
        "is_safe_to_fetch": source.is_safe_to_fetch,
        "last_status": source.last_status,
    }


def main(data: dict[str, Any]) -> dict[str, Any]:
    """入力検証・モデル生成・辞書整形の一連の処理を実行する。

    Args:
        data (dict[str, Any]): RSS取得元データ辞書。

    Returns:
        dict[str, Any]: 整形済み辞書。

    Side Effects:
        なし。

    Examples:
        >>> main({"name": "NHK", "url": "https://www.nhk.or.jp/rss/news/cat0.xml"})["name"]
        'NHK'
    """
    validated = validate_input(data)
    instance = execute_logic(validated)
    return format_output(instance)
