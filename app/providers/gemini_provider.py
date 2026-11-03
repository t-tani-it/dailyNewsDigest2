"""Google AI Studio（Gemini API）用 LLM Provider（Adapter）モジュール。

目的（Why）:
    `google-genai` SDK を用いて Gemini API と通信し、
    「請求先未設定（Free Tier）が確認されたプロジェクト」かつ「Free Tier 許可リストに含まれるモデル」
    のみを実行する安全ガードと、構造化JSON出力・トークン使用量取得を提供する
    （指示書 §4.2, §10.2, §11, §12.1, §14, §15）。
仕様（What）:
    - `GeminiProvider`: `BaseLlmProvider` を継承した Gemini アダプタ
    - `is_gemini_model_in_allowlist`: `models/` プレフィックス有無を吸収して許可リストと照合
    - `extract_gemini_usage`: `usage_metadata` から入力・出力・推論・合計トークンを抽出（未計測を0にしない）
    - `list_available_models`: `models.list` API結果と Free Tier 許可リストを照合し `DiscoveredModelSchema` を生成
前提条件・依存関係（Assumption / Dependencies）:
    google-genai>=1.0, pydantic>=2.7, `app.core.config`, `app.providers.base_provider`, `app.schemas.llm_schema`
入出力（I/O）:
    入力: モデルID、プロンプト、検証用Pydanticスキーマ
    出力: `LlmProviderCallResultSchema` または `list[DiscoveredModelSchema]`
注意点（Caution）:
    - `models.list` に存在するだけのモデルを自動で無料利用可と判定してはならない（指示書 §10.2）。
    - `gemini_free_tier_confirmed` が False の場合は API 呼び出しを行わない。
    - APIキーはログや例外文言へ絶対に出力しない。
今後の拡張ポイント（Future Work）:
    レスポンスヘッダーからの残クォータ推定情報の取得。
変更履歴（Change Log）:
    2026-11-03 - 初版作成（Step 4-1）
"""

from __future__ import annotations

from typing import Any

from google import genai
from google.genai import types
from pydantic import BaseModel

from app.core.config import (
    GEMINI_FREE_TIER_ALLOWLIST,
    get_settings,
)
from app.core.logging import get_logger, mask_sensitive_text
from app.models.model_catalog import (
    AVAILABILITY_AVAILABLE,
    FREE_STATUS_PAID_OR_CONDITIONAL,
    FREE_STATUS_UNVERIFIED,
    FREE_STATUS_VERIFIED,
)
from app.providers.base_provider import BaseLlmProvider, ProviderHttpError
from app.schemas.llm_schema import (
    PAID_COMPARISON_MODEL_IDS,
    DiscoveredModelSchema,
    LlmTokenUsageSchema,
)

# =============================================================================
# 設定値ブロック（Gemini 生成パラメータ定数）
# =============================================================================

PROVIDER_NAME_GEMINI: str = "gemini"
DEFAULT_GEMINI_TEMPERATURE: float = 0.1

logger = get_logger(__name__)


def normalize_gemini_model_id(model_id: str) -> str:
    """Gemini モデルIDから先頭の `models/` プレフィックスを除去して正規化する。

    Args:
        model_id (str): `"models/gemini-2.0-flash"` または `"gemini-2.0-flash"`。

    Returns:
        str: `"gemini-2.0-flash"` 形式の短いモデルID。

    Side Effects:
        なし。

    Examples:
        >>> normalize_gemini_model_id("models/gemini-2.0-flash")
        'gemini-2.0-flash'
    """
    cleaned = (model_id or "").strip()
    if cleaned.startswith("models/"):
        return cleaned[len("models/") :]
    return cleaned


def is_gemini_model_in_allowlist(
    model_id: str,
    allowlist: tuple[str, ...] = GEMINI_FREE_TIER_ALLOWLIST,
) -> bool:
    """指定されたGeminiモデルが Free Tier 許可リストに含まれるか判定する（指示書 §10.2）。

    解説：
        Google AI Studio の `models.list` API は有料専用のモデルも同じ一覧で返す。
        そのため一覧に載っているだけでは無料と判断せず、公式料金表で Free Tier 対象と
        確認された `allowlist` に一致し、かつ有料比較モデルでない場合のみ True とする。

    Args:
        model_id (str): 検査対象のモデルID。
        allowlist (tuple[str, ...]): Free Tier 許可リスト。

    Returns:
        bool: 許可リストに含まれる無料モデルなら True。

    Side Effects:
        なし。

    Examples:
        >>> is_gemini_model_in_allowlist("gemini-2.0-flash")
        True
        >>> is_gemini_model_in_allowlist("gemini-3.8-flash")
        False
    """
    raw = (model_id or "").strip()
    short_id = normalize_gemini_model_id(raw)
    if not short_id or raw in PAID_COMPARISON_MODEL_IDS or short_id in PAID_COMPARISON_MODEL_IDS:
        return False

    normalized_allowlist = {normalize_gemini_model_id(item) for item in allowlist}
    return short_id in normalized_allowlist


def extract_gemini_usage(usage_metadata: Any) -> LlmTokenUsageSchema:
    """Gemini レスポンスの `usage_metadata` からトークン使用量を抽出する（指示書 §15.1）。

    解説：
        - `prompt_token_count`: 入力トークン数
        - `candidates_token_count`: 出力トークン数
        - `thoughts_token_count`: 推論（思考）トークン数
        - `total_token_count`: 合計トークン数
        取得できない項目は `0` にせず `None` のまま渡し、`LlmTokenUsageSchema` 側で
        `unmeasured`（未計測）として扱う。

    Args:
        usage_metadata (Any): `response.usage_metadata` オブジェクトまたは辞書。

    Returns:
        LlmTokenUsageSchema: トークン使用量スキーマ。

    Side Effects:
        なし。

    Examples:
        >>> extract_gemini_usage(None).usage_status
        'unmeasured'
    """
    if usage_metadata is None:
        return LlmTokenUsageSchema()

    def _get_int(field_name: str) -> int | None:
        if isinstance(usage_metadata, dict):
            val = usage_metadata.get(field_name)
        else:
            val = getattr(usage_metadata, field_name, None)
        return int(val) if val is not None else None

    prompt_tokens = _get_int("prompt_token_count")
    candidates_tokens = _get_int("candidates_token_count")
    thoughts_tokens = _get_int("thoughts_token_count")
    total_tokens = _get_int("total_token_count")

    # Gemini の total_token_count は prompt + candidates (+ thoughts) を含むため、
    # thoughts が別計上されている場合は reasoning_included_in_output=False として整合させる
    reasoning_included = True
    if (
        prompt_tokens is not None
        and candidates_tokens is not None
        and thoughts_tokens
        and total_tokens == prompt_tokens + candidates_tokens + thoughts_tokens
    ):
        reasoning_included = False

    return LlmTokenUsageSchema(
        input_tokens=prompt_tokens,
        output_tokens=candidates_tokens,
        reasoning_tokens=thoughts_tokens,
        total_tokens=total_tokens,
        reasoning_included_in_output=reasoning_included,
    )


class GeminiProvider(BaseLlmProvider):
    """Google AI Studio（Gemini API）Free Tier 専用アダプタ。"""

    def __init__(
        self,
        api_key: str | None = None,
        enabled: bool | None = None,
        free_tier_confirmed: bool | None = None,
        allowlist: tuple[str, ...] = GEMINI_FREE_TIER_ALLOWLIST,
        genai_client: Any | None = None,
    ) -> None:
        """GeminiProvider を初期化する。

        Args:
            api_key (str | None): Google AI Studio APIキー（省略時は Settings から取得）。
            enabled (bool | None): Gemini有効フラグ（省略時は Settings から取得）。
            free_tier_confirmed (bool | None): 請求先未設定（Free Tier）確認済みフラグ。
            allowlist (tuple[str, ...]): Free Tier 許可モデルリスト。
            genai_client (Any | None): テスト用に注入する `genai.Client` 互換オブジェクト。
        """
        cfg = get_settings()
        self._api_key = cfg.gemini_api_key if api_key is None else api_key
        self._enabled = cfg.gemini_enabled if enabled is None else enabled
        self._free_tier_confirmed = (
            cfg.gemini_free_tier_confirmed
            if free_tier_confirmed is None
            else free_tier_confirmed
        )
        self._allowlist = allowlist
        self._client = genai_client
        self._timeout_seconds = cfg.llm_request_timeout_seconds

    @property
    def provider_name(self) -> str:
        """プロバイダー名 `"gemini"` を返す。"""
        return PROVIDER_NAME_GEMINI

    def is_configured(self) -> bool:
        """APIキー設定済み・Gemini有効・Free Tier確認済みの3条件をすべて満たすか返す。"""
        return bool(
            self._enabled
            and self._free_tier_confirmed
            and self._api_key
            and self._api_key.strip()
        )

    def verify_model_is_free(
        self,
        model_id: str,
        catalog_meta: dict[str, Any] | None = None,
    ) -> bool:
        """モデルが Gemini Free Tier 許可リストに含まれ、料金が0以下であることを検証する。"""
        if not self._free_tier_confirmed:
            return False
        if not is_gemini_model_in_allowlist(model_id, allowlist=self._allowlist):
            return False

        if catalog_meta:
            for price_key in ("input_price_per_1m", "output_price_per_1m", "request_price"):
                price_val = catalog_meta.get(price_key)
                if price_val is not None and float(price_val) > 0.0:
                    return False
            status = catalog_meta.get("free_tier_status")
            if status and status != FREE_STATUS_VERIFIED:
                return False

        return True

    def _get_client(self) -> Any:
        """`google.genai.Client` インスタンスを取得（遅延生成）する。"""
        if self._client is not None:
            return self._client
        if not self._api_key or not self._api_key.strip():
            raise ProviderHttpError("GEMINI_API_KEY が設定されていません。", http_status=401)
        self._client = genai.Client(api_key=self._api_key.strip())
        return self._client

    def list_available_models(self) -> list[DiscoveredModelSchema]:
        """`models.list` からモデル一覧を取得し、Free Tier 許可リストと照合して返す（指示書 §12.1）。

        解説：
            APIから返されたモデルであっても、許可リストに無いものは `unverified`（要確認）、
            有料比較モデルは `paid_or_conditional` とし、許可リスト一致モデルのみ
            `verified_free` として返す。

        Returns:
            list[DiscoveredModelSchema]: 検出されたモデル一覧。

        Side Effects:
            `models.list` APIへのHTTP GET通信を行う。
        """
        if not self._api_key or not self._api_key.strip():
            return []

        try:
            client = self._get_client()
            raw_models = list(client.models.list())
        except Exception as exc:
            logger.warning(
                "Gemini models.list の取得に失敗しました: %s",
                mask_sensitive_text(str(exc)),
            )
            return []

        discovered: list[DiscoveredModelSchema] = []
        for item in raw_models:
            raw_name = str(getattr(item, "name", "") or "").strip()
            short_id = normalize_gemini_model_id(raw_name)
            if not short_id:
                continue

            supported_actions = getattr(item, "supported_actions", None)
            if supported_actions and "generateContent" not in supported_actions:
                continue

            display_name = str(getattr(item, "display_name", "") or short_id)
            input_limit = getattr(item, "input_token_limit", None)

            if short_id in PAID_COMPARISON_MODEL_IDS or raw_name in PAID_COMPARISON_MODEL_IDS:
                free_status = FREE_STATUS_PAID_OR_CONDITIONAL
            elif self._free_tier_confirmed and is_gemini_model_in_allowlist(
                short_id, allowlist=self._allowlist
            ):
                free_status = FREE_STATUS_VERIFIED
            else:
                free_status = FREE_STATUS_UNVERIFIED

            discovered.append(
                DiscoveredModelSchema(
                    model_id=short_id,
                    provider=PROVIDER_NAME_GEMINI,
                    model_name=display_name,
                    free_tier_status=free_status,
                    input_price_per_1m=0.0 if free_status == FREE_STATUS_VERIFIED else None,
                    output_price_per_1m=0.0 if free_status == FREE_STATUS_VERIFIED else None,
                    request_price=0.0 if free_status == FREE_STATUS_VERIFIED else None,
                    context_length=int(input_limit) if input_limit is not None else None,
                    modalities=["text"],
                    supports_structured_output=True,
                    availability_status=AVAILABILITY_AVAILABLE,
                    source_info="google_genai_models_list",
                )
            )

        return discovered

    def _call_api(
        self,
        model_id: str,
        system_prompt: str,
        user_prompt: str,
        response_schema: type[BaseModel],
    ) -> tuple[str, LlmTokenUsageSchema, int | None]:
        """`google-genai` の `models.generate_content` を呼び出す。"""
        client = self._get_client()
        short_id = normalize_gemini_model_id(model_id)

        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            response_schema=response_schema,
            temperature=DEFAULT_GEMINI_TEMPERATURE,
        )

        response = client.models.generate_content(
            model=short_id,
            contents=user_prompt,
            config=config,
        )

        raw_text = str(getattr(response, "text", "") or "")
        usage_meta = getattr(response, "usage_metadata", None)
        usage = extract_gemini_usage(usage_meta)
        return raw_text, usage, 200


def validate_input(data: dict[str, Any]) -> dict[str, Any]:
    """Gemini 無料判定テスト用の入力辞書を検証する。

    Args:
        data (dict[str, Any]): `model_id` を含む辞書。

    Returns:
        dict[str, Any]: 検証済み辞書。

    Raises:
        ValueError: `model_id` が無い場合。

    Side Effects:
        なし。

    Examples:
        >>> validate_input({"model_id": "gemini-2.0-flash"})["model_id"]
        'gemini-2.0-flash'
    """
    if not isinstance(data, dict) or not data.get("model_id"):
        raise ValueError("model_id を含む辞書が必要です。")
    return dict(data)


def execute_logic(validated: dict[str, Any]) -> dict[str, Any]:
    """指定モデルIDが Gemini Free Tier 許可リストに含まれるか判定する。"""
    model_id = str(validated["model_id"])
    short_id = normalize_gemini_model_id(model_id)
    allowed = is_gemini_model_in_allowlist(model_id)
    return {
        "model_id": short_id,
        "is_in_free_allowlist": allowed,
    }


def format_output(result: dict[str, Any]) -> dict[str, Any]:
    """結果辞書を整形して返す。"""
    return dict(result)


def main(data: dict[str, Any]) -> dict[str, Any]:
    """Gemini モデル無料許可判定を実行する。"""
    validated = validate_input(data)
    res = execute_logic(validated)
    return format_output(res)
