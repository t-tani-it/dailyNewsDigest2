"""Gemini API と OpenRouter API で共通利用する LLM 入出力・トークン使用量 Pydantic スキーマ。

目的（Why）:
    異なるLLMプロバイダー（Gemini / OpenRouter）の出力形式やトークン使用量メタデータの差異を吸収し、
    単一の型安全なデータ構造で分類・重要度判定・日本語要約・トークン記録を行えるようにする
    （指示書 §4.2, §7.1, §9.1, §14, §15）。
仕様（What）:
    - `ArticleClassificationItemSchema` / `ArticleClassificationBatchResponseSchema`: AIカテゴリ分類・重要度・除外判定
    - `ArticleSummaryItemSchema` / `ArticleSummaryBatchResponseSchema`: AI日本語要約・「なぜ重要なのか」・「概要情報不足」制御
    - `LlmTokenUsageSchema`: 未計測（`None`）を `0` と混同せず、推論トークンの二重計上を防ぐ使用量スキーマ
    - `LlmProviderCallResultSchema`: Provider呼び出しの成否・検証済みデータ・使用量・エラー種別を統一保持
    - `DiscoveredModelSchema`: モデル一覧APIから取得したモデル情報と無料確認状態スキーマ
    - `parse_and_validate_llm_json`: コードブロック除去・JSON抽出・Pydantic検証を一括実行するヘルパー
前提条件・依存関係（Assumption / Dependencies）:
    pydantic>=2.7, `app.core.config`, `app.models.analysis`, `app.models.model_catalog`, `app.models.usage_log`
入出力（I/O）:
    入力: LLMが返した生テキスト（JSON文字列）およびAPIレスポンスのusageメタデータ辞書
    出力: 検証済み Pydantic モデルまたは整形済み辞書
注意点（Caution）:
    - タイトル・概要だけで正確に要約できない場合は推測補完せず `summary_ja="概要情報不足"` とする（指示書 §7.1）。
    - トークン使用量が取得できない場合は `0` ではなく `None`（`usage_status="unmeasured"`）とする（指示書 §15.1）。
    - 曖昧なカテゴリ出力はエラーで捨てず `"unclassified"`（分類不能）へ安全にフォールバックする（指示書 §2.2）。
今後の拡張ポイント（Future Work）:
    複数記事間の比較コメントやキーワードタグ抽出フィールドの追加。
変更履歴（Change Log）:
    2026-11-03 - 初版作成（Step 4-1）
"""

from __future__ import annotations

import json
import re
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.config import (
    COMPARISON_PAID_MODELS,
    EXCLUDED_CATEGORIES,
    NEWS_CATEGORIES,
)
from app.models.analysis import INSUFFICIENT_INFO_TEXT
from app.models.model_catalog import (
    AVAILABILITY_AVAILABLE,
    FREE_STATUS_PAID_OR_CONDITIONAL,
    FREE_STATUS_UNVERIFIED,
)
from app.models.usage_log import USAGE_STATUS_MEASURED, USAGE_STATUS_UNMEASURED

# =============================================================================
# 設定値ブロック（LLM出力検証・エラー分類定数）
# =============================================================================

VALID_CATEGORY_KEYS: tuple[str, ...] = tuple(NEWS_CATEGORIES.keys())
VALID_EXCLUSION_REASONS: tuple[str, ...] = tuple(EXCLUDED_CATEGORIES.keys())

# 有料比較対象3モデルのID集合（API呼び出し禁止ガードで使用：指示書 §11, §16）
PAID_COMPARISON_MODEL_IDS: frozenset[str] = frozenset(
    str(item["model_id"]) for item in COMPARISON_PAID_MODELS
)

# Providerエラー種別定数（自動モデル切替・保留判断に使用：指示書 §12.3）
LLM_ERROR_RATE_LIMIT: str = "rate_limit"
LLM_ERROR_QUOTA_EXHAUSTED: str = "quota_exhausted"
LLM_ERROR_AUTH: str = "auth_error"
LLM_ERROR_MODEL_UNAVAILABLE: str = "model_unavailable"
LLM_ERROR_VALIDATION: str = "validation_error"
LLM_ERROR_POLICY_VIOLATION: str = "policy_violation"
LLM_ERROR_NETWORK: str = "network_error"

# 自動切替（フォールバック）の対象となるエラー種別
SWITCHABLE_LLM_ERROR_TYPES: frozenset[str] = frozenset(
    {
        LLM_ERROR_RATE_LIMIT,
        LLM_ERROR_QUOTA_EXHAUSTED,
        LLM_ERROR_AUTH,
        LLM_ERROR_MODEL_UNAVAILABLE,
        LLM_ERROR_VALIDATION,
        LLM_ERROR_NETWORK,
    }
)

CODE_FENCE_PATTERN: re.Pattern[str] = re.compile(
    r"```(?:json|JSON)?\s*(.*?)\s*```", re.DOTALL
)

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class ArticleClassificationInputSchema(BaseModel):
    """AIカテゴリ分類・重要度判定へ渡す1記事分の入力スキーマ。"""

    article_id: int
    title: str
    source_name: str
    summary_snippet: str = ""
    rss_categories: list[str] = Field(default_factory=list)


class ArticleClassificationItemSchema(BaseModel):
    """AIによる1記事分のカテゴリ分類・除外判定・重要度スコア出力スキーマ。

    解説：
        LLMが定義外のカテゴリ文字列を返した場合でも、記事全体の処理を失敗させず
        安全側の `"unclassified"`（分類不能）に正規化する（指示書 §2.2）。
        また重要度スコアが 0〜100 をわずかに外れた場合も範囲内に丸める。
    """

    model_config = ConfigDict(extra="ignore")

    article_id: int
    category: str = "unclassified"
    is_excluded: bool = False
    exclusion_reason: str | None = None
    importance_score: int = Field(default=50, ge=0, le=100)
    reason: str = ""

    @field_validator("category", mode="before")
    @classmethod
    def normalize_category(cls, value: Any) -> str:
        """未知のカテゴリキーを `unclassified` へ正規化する。"""
        if not value:
            return "unclassified"
        cat_str = str(value).strip()
        if cat_str in NEWS_CATEGORIES:
            return cat_str
        return "unclassified"

    @field_validator("importance_score", mode="before")
    @classmethod
    def clamp_importance_score(cls, value: Any) -> int:
        """重要度スコアを 0〜100 の整数へ丸める。"""
        try:
            score = int(float(value))
        except (TypeError, ValueError):
            return 50
        return max(0, min(100, score))

    @field_validator("exclusion_reason", mode="before")
    @classmethod
    def normalize_exclusion_reason(cls, value: Any) -> str | None:
        """除外理由を検証し、空文字列なら None にする。"""
        if value is None:
            return None
        reason_str = str(value).strip()
        if not reason_str:
            return None
        return reason_str


class ArticleClassificationBatchResponseSchema(BaseModel):
    """複数記事のAI分類・重要度判定バッチ応答スキーマ。"""

    model_config = ConfigDict(extra="ignore")

    items: list[ArticleClassificationItemSchema] = Field(default_factory=list)


class ArticleSummaryInputSchema(BaseModel):
    """AI日本語要約へ渡す1記事分の入力スキーマ。"""

    article_id: int
    title: str
    source_name: str
    summary_snippet: str = ""
    category: str = "unclassified"


class ArticleSummaryItemSchema(BaseModel):
    """AIによる1記事分の日本語要約・「なぜ重要なのか」出力スキーマ（指示書 §7.1, §17.2）。

    解説：
        タイトル・概要だけでは正確に要約できない場合、LLMには推測させず
        `is_insufficient_info=True` または `summary_ja="概要情報不足"` を返させる。
        本スキーマでも空文字や情報不足フラグを検知し、必ず `"概要情報不足"` に統一する。
    """

    model_config = ConfigDict(extra="ignore")

    article_id: int
    summary_ja: str = INSUFFICIENT_INFO_TEXT
    why_important: str = INSUFFICIENT_INFO_TEXT
    is_insufficient_info: bool = False

    @model_validator(mode="after")
    def enforce_insufficient_info_rule(self) -> ArticleSummaryItemSchema:
        """情報不足時または空要約時に推測補完を防ぎ『概要情報不足』へ統一する。"""
        cleaned_summary = (self.summary_ja or "").strip()
        cleaned_why = (self.why_important or "").strip()

        if (
            self.is_insufficient_info
            or not cleaned_summary
            or cleaned_summary == INSUFFICIENT_INFO_TEXT
        ):
            self.is_insufficient_info = True
            self.summary_ja = INSUFFICIENT_INFO_TEXT
            if not cleaned_why:
                self.why_important = INSUFFICIENT_INFO_TEXT
            else:
                self.why_important = cleaned_why
        else:
            self.summary_ja = cleaned_summary
            self.why_important = cleaned_why if cleaned_why else INSUFFICIENT_INFO_TEXT

        return self


class ArticleSummaryBatchResponseSchema(BaseModel):
    """複数記事のAI日本語要約バッチ応答スキーマ。"""

    model_config = ConfigDict(extra="ignore")

    items: list[ArticleSummaryItemSchema] = Field(default_factory=list)


class LlmTokenUsageSchema(BaseModel):
    """1回のLLM API呼び出しにおけるトークン使用量スキーマ（指示書 §14, §15）。

    解説：
        1. APIからトークン数が返されなかった場合は `0` ではなく `None` のまま保持し、
           `usage_status="unmeasured"`（未計測）とする。
        2. 推論トークン（`reasoning_tokens`）が出力トークン（`output_tokens`）に
           内包されている場合（`reasoning_included_in_output=True`）は、
           `total_tokens` 計算時に推論トークンを二重加算しない。
    """

    model_config = ConfigDict(extra="ignore")

    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    reasoning_included_in_output: bool = True
    usage_status: str = USAGE_STATUS_UNMEASURED

    @model_validator(mode="after")
    def compute_safe_totals(self) -> LlmTokenUsageSchema:
        """未計測判定と推論トークン二重計上防止の合算処理を行う。"""
        if self.input_tokens is not None and self.output_tokens is not None:
            in_val = max(0, int(self.input_tokens))
            out_val = max(0, int(self.output_tokens))
            self.input_tokens = in_val
            self.output_tokens = out_val

            if self.reasoning_tokens is not None:
                self.reasoning_tokens = max(0, int(self.reasoning_tokens))

            if self.total_tokens is not None:
                # もしAPI返却の total_tokens が input + output + reasoning になっており、
                # かつ reasoning_included_in_output=True の場合は二重加算を補正する
                raw_total = max(0, int(self.total_tokens))
                if (
                    self.reasoning_included_in_output
                    and self.reasoning_tokens
                    and raw_total == in_val + out_val + self.reasoning_tokens
                ):
                    self.total_tokens = in_val + out_val
                else:
                    self.total_tokens = raw_total
            elif not self.reasoning_included_in_output and self.reasoning_tokens:
                self.total_tokens = in_val + out_val + self.reasoning_tokens
            else:
                self.total_tokens = in_val + out_val

            self.usage_status = USAGE_STATUS_MEASURED
        else:
            self.usage_status = USAGE_STATUS_UNMEASURED

        return self


class LlmProviderCallResultSchema(BaseModel):
    """Provider（Gemini / OpenRouter）呼び出しの統一結果スキーマ。"""

    model_config = ConfigDict(extra="ignore")

    success: bool
    provider: str
    model_id: str
    purpose: str = "classify"
    parsed_data: dict[str, Any] | None = None
    raw_text: str = ""
    usage: LlmTokenUsageSchema = Field(default_factory=LlmTokenUsageSchema)
    http_status: int | None = None
    error_type: str | None = None
    error_message: str | None = None
    attempt_count: int = 1

    @property
    def is_switchable_error(self) -> bool:
        """自動モデル切替（別無料モデルへのフォールバック）対象エラーかを返す。"""
        if self.success:
            return False
        return (self.error_type or "") in SWITCHABLE_LLM_ERROR_TYPES


class DiscoveredModelSchema(BaseModel):
    """Google AI Studio / OpenRouter から取得したモデル情報スキーマ（指示書 §12）。"""

    model_config = ConfigDict(extra="ignore")

    model_id: str
    provider: str
    model_name: str
    free_tier_status: str = FREE_STATUS_UNVERIFIED
    input_price_per_1m: float | None = None
    output_price_per_1m: float | None = None
    request_price: float | None = None
    context_length: int | None = None
    modalities: list[str] = Field(default_factory=lambda: ["text"])
    supports_structured_output: bool = False
    availability_status: str = AVAILABILITY_AVAILABLE
    source_info: str = "api"

    @model_validator(mode="after")
    def enforce_zero_cost_for_verified_free(self) -> DiscoveredModelSchema:
        """正の料金が設定されているモデルや有料比較モデルは絶対に `verified_free` にしない。"""
        prices = (
            self.input_price_per_1m,
            self.output_price_per_1m,
            self.request_price,
        )
        if self.model_id in PAID_COMPARISON_MODEL_IDS or any(
            p is not None and float(p) > 0.0 for p in prices
        ):
            self.free_tier_status = FREE_STATUS_PAID_OR_CONDITIONAL
        return self


def extract_json_text(raw_text: str) -> str:
    """LLMの応答テキストからMarkdownコードブロック等を除去しJSON文字列を抽出する。

    解説：
        無料LLMモデルによっては、JSON出力を指示しても前後に説明文や
        ````json ... ```` の囲みを付けることがある。
        そこで最初にコードフェンス内を探し、無ければ最初の `{` または `[` から
        最後の `}` または `]` までを切り出すことでパース失敗を防ぐ。

    Args:
        raw_text (str): LLMが返した生の文字列。

    Returns:
        str: 抽出されたJSON文字列。

    Raises:
        ValueError: 空文字列またはJSON構造が見つからない場合。

    Side Effects:
        なし。

    Examples:
        >>> extract_json_text("```json\\n{\\"items\\": []}\\n```")
        '{"items": []}'
    """
    if not raw_text or not raw_text.strip():
        raise ValueError("LLMからの応答テキストが空です。")

    text = raw_text.strip()
    fence_match = CODE_FENCE_PATTERN.search(text)
    if fence_match:
        text = fence_match.group(1).strip()

    first_brace = text.find("{")
    first_bracket = text.find("[")

    if first_brace == -1 and first_bracket == -1:
        raise ValueError("LLM応答内にJSONオブジェクトまたは配列が見つかりません。")

    if first_brace != -1 and (first_bracket == -1 or first_brace < first_bracket):
        last_brace = text.rfind("}")
        if last_brace == -1 or last_brace < first_brace:
            raise ValueError("JSONオブジェクトの閉じ括弧 '}' が見つかりません。")
        return text[first_brace : last_brace + 1]

    last_bracket = text.rfind("]")
    if last_bracket == -1 or last_bracket < first_bracket:
        raise ValueError("JSON配列の閉じ括弧 ']' が見つかりません。")
    return text[first_bracket : last_bracket + 1]


def parse_and_validate_llm_json(
    raw_text: str,
    schema_cls: type[SchemaT],
) -> SchemaT:
    """LLMの応答テキストからJSONを抽出し、指定されたPydanticスキーマで検証する。

    Args:
        raw_text (str): LLMが返した生テキスト。
        schema_cls (type[SchemaT]): 検証先のPydanticモデルクラス。

    Returns:
        SchemaT: 検証済みのPydanticモデルインスタンス。

    Raises:
        ValueError: JSON構文エラーまたはPydantic検証エラー時。

    Side Effects:
        なし。

    Examples:
        >>> raw = '{"items": [{"article_id": 1, "category": "ai_llm", "importance_score": 85}]}'
        >>> res = parse_and_validate_llm_json(raw, ArticleClassificationBatchResponseSchema)
        >>> res.items[0].category
        'ai_llm'
    """
    json_str = extract_json_text(raw_text)
    try:
        parsed: Any = json.loads(json_str)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM応答のJSON解析に失敗しました: {exc}") from exc

    # LLMが {"items": [...]} ではなく直接 [...] の配列を返した場合でも吸収する
    if isinstance(parsed, list) and "items" in schema_cls.model_fields:
        parsed = {"items": parsed}

    try:
        return schema_cls.model_validate(parsed)
    except Exception as exc:
        raise ValueError(f"LLM応答がスキーマ定義と一致しません: {exc}") from exc


def validate_input(data: dict[str, Any]) -> dict[str, Any]:
    """スキーマ検証用の入力辞書をチェックする。

    Args:
        data (dict[str, Any]): `raw_text` と `purpose` を含む辞書。

    Returns:
        dict[str, Any]: 検証済み入力辞書。

    Raises:
        ValueError: `raw_text` が存在しない場合。

    Side Effects:
        なし。

    Examples:
        >>> validate_input({"raw_text": '{"items": []}', "purpose": "classify"})["purpose"]
        'classify'
    """
    if not isinstance(data, dict) or "raw_text" not in data:
        raise ValueError("raw_text を含む辞書が必要です。")
    return dict(data)


def execute_logic(validated: dict[str, Any]) -> BaseModel:
    """用途（`classify` または `summarize`）に応じたPydanticスキーマでJSONを検証する。

    Args:
        validated (dict[str, Any]): `validate_input` 通過後の辞書。

    Returns:
        BaseModel: 検証済みバッチ応答スキーマ。

    Side Effects:
        なし。

    Examples:
        >>> obj = execute_logic({"raw_text": '{"items": [{"article_id": 1, "category": "ai_llm"}]}', "purpose": "classify"})
        >>> len(obj.items)
        1
    """
    purpose = str(validated.get("purpose", "classify"))
    raw_text = str(validated["raw_text"])
    if purpose == "summarize":
        return parse_and_validate_llm_json(raw_text, ArticleSummaryBatchResponseSchema)
    return parse_and_validate_llm_json(raw_text, ArticleClassificationBatchResponseSchema)


def format_output(model_obj: BaseModel) -> dict[str, Any]:
    """検証済みPydanticモデルを辞書へ変換する。

    Args:
        model_obj (BaseModel): 検証済みモデル。

    Returns:
        dict[str, Any]: シリアライズ可能な辞書。

    Side Effects:
        なし。

    Examples:
        >>> out = format_output(execute_logic({"raw_text": '{"items": []}', "purpose": "classify"}))
        >>> out["items"]
        []
    """
    return model_obj.model_dump()


def main(data: dict[str, Any]) -> dict[str, Any]:
    """LLM応答テキストのJSON抽出・スキーマ検証・辞書整形を一括実行する。

    Args:
        data (dict[str, Any]): `raw_text`, `purpose` を含む辞書。

    Returns:
        dict[str, Any]: 検証・整形済み辞書。

    Side Effects:
        なし。

    Examples:
        >>> main({"raw_text": '{"items": [{"article_id": 1, "summary_ja": ""}]}', "purpose": "summarize"})["items"][0]["summary_ja"]
        '概要情報不足'
    """
    validated = validate_input(data)
    model_obj = execute_logic(validated)
    return format_output(model_obj)
