"""ニュース記事・RSS取得元・初回起動確認用のPydanticスキーマ。

目的（Why）:
    RSS取得元、ニュースカード表示データ、および初回起動時の「はい・いいえ・キャンセル」
    確認フローの入出力データを型安全に検証する（指示書 §6, §17.2）。
仕様（What）:
    - `ArticleCardSchema`: 記事単位の表示情報（トークン・料金フィールドを含めない）
    - `OnboardingPreviewRequestSchema` / `OnboardingPreviewResponseSchema`: 初回確認用
    - `OnboardingDecisionRequestSchema`: 「はい / いいえ / キャンセル」選択検証用
前提条件・依存関係（Assumption / Dependencies）:
    pydantic>=2.7, `app.core.config`
入出力（I/O）:
    Controller と Service / View 間の検証済みモデル。
注意点（Caution）:
    `ArticleCardSchema` にはトークン使用量や有料モデルの参考料金を絶対に含めないこと（指示書 §17.2）。
今後の拡張ポイント（Future Work）:
    関連記事（別メディア報道）リストのネスト表示スキーマ。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 2-1）
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.config import DEFAULT_SELECTED_CATEGORIES, NEWS_CATEGORIES

# =============================================================================
# 設定値ブロック（初回確認時の注意事項5項目：指示書 §6.3）
# =============================================================================

ONBOARDING_NOTICES: tuple[str, ...] = (
    "RSSの件数確認時点で外部サイトへの通信が発生すること",
    "AI分析ではタイトルや許可された概要を外部LLMへ送信すること",
    "RSSの公開範囲外の記事は取得できないこと",
    "無料APIの利用制限によりAI処理が実行できない場合があること",
    "有料APIは実行しないこと",
)


class RssSourceSchema(BaseModel):
    """RSS取得元情報の表示・応答用スキーマ。"""

    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    name: str
    url: str
    category_hint: str = "unclassified"
    is_enabled: bool = True
    terms_status: str = "unverified"
    allow_store_summary: bool = False
    allow_ai_summary: bool = False
    last_fetched_at: datetime | None = None
    last_status: str = "never"


class ArticleCardSchema(BaseModel):
    """Web画面のニュースカード1枚に対応するスキーマ（指示書 §17.2）。

    解説：
        ニュースカードには記事単位の情報のみを保持し、
        トークン使用量や有料モデルの推定料金フィールドは一切持たせない。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    source_name: str
    url: str
    published_at: datetime | None = None
    published_at_jst_str: str = "日時不明"
    updated_at_rss: datetime | None = None
    updated_at_jst_str: str | None = None
    category: str = "unclassified"
    category_label: str = "分類不能"
    importance_score: int = Field(default=50, ge=0, le=100)
    summary_ja: str = "（AI要約未実行）"
    why_important: str = "（AI分析未実行）"
    is_read: bool = False
    is_reused: bool = False
    cluster_id: str | None = None
    related_sources_count: int = 1


class OnboardingPreviewRequestSchema(BaseModel):
    """初回起動時の対象日・カテゴリ指定リクエストスキーマ（指示書 §6.1, §6.2）。"""

    target_date: date
    selected_categories: list[str] = Field(
        default_factory=lambda: list(DEFAULT_SELECTED_CATEGORIES),
        min_length=1,
    )
    select_all: bool = False

    @field_validator("selected_categories")
    @classmethod
    def validate_categories(cls, values: list[str]) -> list[str]:
        """選択されたカテゴリが定義済みカテゴリに含まれるか検証する。

        Args:
            values (list[str]): 選択されたカテゴリキーのリスト。

        Returns:
            list[str]: 重複を除いた有効なカテゴリリスト。

        Raises:
            ValueError: 有効なカテゴリが1つも無い場合。

        Side Effects:
            なし。

        Examples:
            >>> OnboardingPreviewRequestSchema.validate_categories(["ai_llm"])
            ['ai_llm']
        """
        valid = [c for c in dict.fromkeys(values) if c in NEWS_CATEGORIES]
        if not valid:
            raise ValueError("有効なニュースカテゴリを1つ以上選択してください。")
        return valid


class OnboardingPreviewResponseSchema(BaseModel):
    """初回起動時のRSS収集・暫定件数プレビュー結果スキーマ（指示書 §6）。"""

    preview_token: str
    target_date: date
    selected_categories: list[str]
    total_fetched_count: int
    deduped_count: int
    category_tentative_counts: dict[str, int]
    out_of_range_notice: str | None = None
    notices: list[str] = Field(default_factory=lambda: list(ONBOARDING_NOTICES))
    preview_titles: list[dict[str, Any]] = Field(default_factory=list)


class OnboardingDecisionRequestSchema(BaseModel):
    """初回確認の「はい / いいえ / キャンセル」選択リクエストスキーマ（指示書 §6.3）。"""

    preview_token: str
    decision: Literal["yes", "no", "cancel"]
    target_date: date | None = None
    selected_categories: list[str] = Field(
        default_factory=lambda: list(DEFAULT_SELECTED_CATEGORIES)
    )


def validate_input(data: dict[str, Any]) -> dict[str, Any]:
    """記事カード整形用の入力辞書を検証する。

    Args:
        data (dict[str, Any]): 記事属性を含む辞書。

    Returns:
        dict[str, Any]: 検証済み辞書。

    Raises:
        ValueError: `id`, `title`, `source_name`, `url` のいずれかが欠落している場合。

    Side Effects:
        なし。

    Examples:
        >>> validate_input({"id": 1, "title": "T", "source_name": "S", "url": "https://example.com"})["id"]
        1
    """
    for req in ("id", "title", "source_name", "url"):
        if data.get(req) is None or data.get(req) == "":
            raise ValueError(f"ArticleCardSchema には {req} が必須です。")
    return dict(data)


def execute_logic(validated_data: dict[str, Any]) -> ArticleCardSchema:
    """検証済み辞書から `ArticleCardSchema` を構築する。

    Args:
        validated_data (dict[str, Any]): `validate_input` 通過後の辞書。

    Returns:
        ArticleCardSchema: 検証済みPydanticモデル。

    Side Effects:
        なし。

    Examples:
        >>> card = execute_logic({"id": 1, "title": "T", "source_name": "S", "url": "https://example.com", "category": "ai_llm"})
        >>> card.category_label
        '生成AI・LLM・AIエージェント'
    """
    cat_key = str(validated_data.get("category") or validated_data.get("primary_category") or "unclassified")
    cat_label = str(
        NEWS_CATEGORIES.get(cat_key, NEWS_CATEGORIES["unclassified"])["label"]
    )
    return ArticleCardSchema(
        id=int(validated_data["id"]),
        title=str(validated_data["title"]),
        source_name=str(validated_data["source_name"]),
        url=str(validated_data["url"]),
        published_at=validated_data.get("published_at"),
        published_at_jst_str=str(validated_data.get("published_at_jst_str", "日時不明")),
        updated_at_rss=validated_data.get("updated_at_rss"),
        updated_at_jst_str=validated_data.get("updated_at_jst_str"),
        category=cat_key,
        category_label=cat_label,
        importance_score=int(validated_data.get("importance_score", 50)),
        summary_ja=str(validated_data.get("summary_ja") or "（AI要約未実行）"),
        why_important=str(validated_data.get("why_important") or "（AI分析未実行）"),
        is_read=bool(validated_data.get("is_read", False)),
        is_reused=bool(validated_data.get("is_reused", False)),
        cluster_id=validated_data.get("cluster_id"),
        related_sources_count=int(validated_data.get("related_sources_count", 1)),
    )


def format_output(card: ArticleCardSchema) -> dict[str, Any]:
    """`ArticleCardSchema` を辞書へ変換する。

    Args:
        card (ArticleCardSchema): 記事カードスキーマ。

    Returns:
        dict[str, Any]: シリアライズ済み辞書。

    Side Effects:
        なし。

    Examples:
        >>> d = format_output(execute_logic({"id": 1, "title": "T", "source_name": "S", "url": "https://example.com"}))
        >>> "estimated_cost_usd" in d
        False
    """
    return card.model_dump()


def main(data: dict[str, Any]) -> dict[str, Any]:
    """入力検証・スキーマ生成・辞書変換の一連の流れを実行する。

    Args:
        data (dict[str, Any]): 記事データ辞書。

    Returns:
        dict[str, Any]: ニュースカード用辞書。

    Side Effects:
        なし。

    Examples:
        >>> main({"id": 1, "title": "T", "source_name": "S", "url": "https://example.com"})["title"]
        'T'
    """
    validated = validate_input(data)
    card = execute_logic(validated)
    return format_output(card)
