"""OpenRouter API 用 LLM Provider（Adapter）モジュール。

目的（Why）:
    OpenRouter の Chat Completions API および Models API と通信し、
    `:free` モデルまたは `openrouter/free` かつ料金が 0 の無料確認済みモデルのみを実行する
    安全ガードと、トークン使用量（推論トークン内訳含む）の取得を提供する
    （指示書 §4.2, §10.1, §11, §12.1, §14, §15）。
仕様（What）:
    - `OpenRouterProvider`: `BaseLlmProvider` を継承した OpenRouter アダプタ
    - `is_openrouter_model_verified_free`: モデルID規則（`:free` / `openrouter/free`）と0円料金を検証
    - `extract_openrouter_usage`: `usage` 辞書から入力・出力・推論・合計トークンを抽出（二重計上防止）
    - `parse_openrouter_model_entry`: `/api/v1/models` の1エントリを `DiscoveredModelSchema` へ変換
前提条件・依存関係（Assumption / Dependencies）:
    httpx>=0.27, pydantic>=2.7, `app.core.config`, `app.providers.base_provider`, `app.schemas.llm_schema`
入出力（I/O）:
    入力: モデルID、プロンプト、検証用Pydanticスキーマ
    出力: `LlmProviderCallResultSchema` または `list[DiscoveredModelSchema]`
注意点（Caution）:
    - 比較用の有料モデル（`deepseek/deepseek-v4.1-flash`, `openai/gpt-6-luna` 等）は絶対に呼び出さない。
    - `:free` が付いていても料金フィールドが正の値である場合は安全側で実行を拒否する。
    - APIキー（`OPENROUTER_API_KEY`）はログや例外メッセージへ一切出力しない。
今後の拡張ポイント（Future Work）:
    OpenRouter の Generation API を用いた非同期トークン確定値の照会。
変更履歴（Change Log）:
    2026-11-03 - 初版作成（Step 4-1）
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel

from app.core.config import (
    OPENROUTER_FREE_MODEL_IDS,
    OPENROUTER_FREE_SUFFIX,
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
# 設定値ブロック（OpenRouter 通信定数）
# =============================================================================

PROVIDER_NAME_OPENROUTER: str = "openrouter"
DEFAULT_OPENROUTER_TEMPERATURE: float = 0.1
TOKENS_PER_MILLION: float = 1_000_000.0

logger = get_logger(__name__)


def _parse_optional_float(value: Any) -> float | None:
    """文字列や数値を float へ変換し、変換不能または None の場合は None を返す。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_openrouter_per_token_to_per_1m(raw_price: Any) -> float | None:
    """OpenRouter の1トークンあたりUSD単価を100万トークンあたりUSD単価へ換算する。

    解説：
        OpenRouter の `/api/v1/models` が返す `pricing.prompt` や `pricing.completion` は
        「1トークンあたりのドル単価（例: `"0.00000014"` = $0.14 / 1M tokens）」である。
        負の値は無効値（None）として扱い、0.0 は 0.0 のまま返す。

    Args:
        raw_price (Any): OpenRouter `pricing` 内の単価文字列または数値。

    Returns:
        float | None: 100万トークンあたりのUSD単価（不明時は None）。

    Side Effects:
        なし。

    Examples:
        >>> parse_openrouter_per_token_to_per_1m("0")
        0.0
        >>> parse_openrouter_per_token_to_per_1m("0.0000005")
        0.5
    """
    val = _parse_optional_float(raw_price)
    if val is None or val < 0.0:
        return None
    if val == 0.0:
        return 0.0
    return round(val * TOKENS_PER_MILLION, 6)


def is_openrouter_model_verified_free(
    model_id: str,
    catalog_meta: dict[str, Any] | None = None,
) -> bool:
    """指定モデルが OpenRouter で無料実行可能と確認できるか判定する（指示書 §10.1）。

    解説：
        1. 有料比較モデル（`PAID_COMPARISON_MODEL_IDS`）は常に False。
        2. モデルIDが `openrouter/free` または末尾 `:free` であること。
        3. カタログ・APIの料金情報が存在する場合、入力・出力・リクエスト料金がすべて 0 以下であること。
           1つでも正の料金が含まれていれば、名前が `:free` でも安全側で False とする。

    Args:
        model_id (str): 検査対象のモデルID。
        catalog_meta (dict[str, Any] | None): 料金や `free_tier_status` を含む辞書（任意）。

    Returns:
        bool: 無料利用確認済みであれば True。

    Side Effects:
        なし。

    Examples:
        >>> is_openrouter_model_verified_free("openrouter/free")
        True
        >>> is_openrouter_model_verified_free("meta-llama/llama-3.3-70b-instruct:free")
        True
        >>> is_openrouter_model_verified_free("openai/gpt-6-luna")
        False
    """
    cleaned = (model_id or "").strip()
    if not cleaned or cleaned in PAID_COMPARISON_MODEL_IDS:
        return False

    is_free_id = cleaned in OPENROUTER_FREE_MODEL_IDS or cleaned.endswith(
        OPENROUTER_FREE_SUFFIX
    )
    if not is_free_id:
        return False

    if catalog_meta:
        for price_key in ("input_price_per_1m", "output_price_per_1m", "request_price"):
            price_val = _parse_optional_float(catalog_meta.get(price_key))
            if price_val is not None and price_val > 0.0:
                return False

        status = catalog_meta.get("free_tier_status")
        if status and status != FREE_STATUS_VERIFIED:
            return False

    return True


def extract_openrouter_usage(usage_dict: dict[str, Any] | None) -> LlmTokenUsageSchema:
    """OpenRouter レスポンスの `usage` フィールドからトークン使用量を抽出する（指示書 §15.1）。

    解説：
        OpenRouter（OpenAI互換形式）では：
        - `prompt_tokens`: 入力トークン数
        - `completion_tokens`: 出力トークン数（推論トークンを内包する）
        - `completion_tokens_details.reasoning_tokens`: 推論トークン内訳
        - `total_tokens`: 合計トークン数
        `usage` が存在しない、または `prompt_tokens` / `completion_tokens` が欠落している場合は
        `0` とせず `None` のまま返し、`unmeasured`（未計測）として記録する。

    Args:
        usage_dict (dict[str, Any] | None): APIレスポンスの `usage` 辞書。

    Returns:
        LlmTokenUsageSchema: トークン使用量スキーマ。

    Side Effects:
        なし。

    Examples:
        >>> u = extract_openrouter_usage({"prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160})
        >>> (u.input_tokens, u.output_tokens, u.total_tokens, u.usage_status)
        (120, 40, 160, 'measured')
    """
    if not isinstance(usage_dict, dict) or not usage_dict:
        return LlmTokenUsageSchema()

    def _to_int(val: Any) -> int | None:
        if val is None:
            return None
        try:
            return int(val)
        except (TypeError, ValueError):
            return None

    in_tokens = _to_int(usage_dict.get("prompt_tokens"))
    out_tokens = _to_int(usage_dict.get("completion_tokens"))
    tot_tokens = _to_int(usage_dict.get("total_tokens"))

    details = usage_dict.get("completion_tokens_details") or {}
    reas_tokens = _to_int(
        details.get("reasoning_tokens")
        if isinstance(details, dict)
        else None
    )
    if reas_tokens is None:
        reas_tokens = _to_int(usage_dict.get("reasoning_tokens"))

    return LlmTokenUsageSchema(
        input_tokens=in_tokens,
        output_tokens=out_tokens,
        reasoning_tokens=reas_tokens,
        total_tokens=tot_tokens,
        reasoning_included_in_output=True,
    )


def parse_openrouter_model_entry(entry: dict[str, Any]) -> DiscoveredModelSchema | None:
    """OpenRouter `/api/v1/models` の1要素を `DiscoveredModelSchema` へ変換する。

    Args:
        entry (dict[str, Any]): OpenRouter モデルオブジェクト辞書。

    Returns:
        DiscoveredModelSchema | None: 変換されたモデルスキーマ（ID欠落時は None）。

    Side Effects:
        なし。

    Examples:
        >>> m = parse_openrouter_model_entry({"id": "openrouter/free", "name": "Free Router", "pricing": {"prompt": "0", "completion": "0", "request": "0"}})
        >>> m.free_tier_status
        'verified_free'
    """
    model_id = str(entry.get("id") or "").strip()
    if not model_id:
        return None

    model_name = str(entry.get("name") or model_id).strip()
    pricing = entry.get("pricing") if isinstance(entry.get("pricing"), dict) else {}

    in_price_1m = parse_openrouter_per_token_to_per_1m(pricing.get("prompt"))
    out_price_1m = parse_openrouter_per_token_to_per_1m(pricing.get("completion"))
    req_price = _parse_optional_float(pricing.get("request"))
    if req_price is not None and req_price < 0.0:
        req_price = None

    has_positive_price = any(
        p is not None and p > 0.0 for p in (in_price_1m, out_price_1m, req_price)
    )
    all_zero_price = (
        in_price_1m == 0.0
        and out_price_1m == 0.0
        and (req_price is None or req_price == 0.0)
    )

    if model_id in PAID_COMPARISON_MODEL_IDS or has_positive_price:
        free_status = FREE_STATUS_PAID_OR_CONDITIONAL
    elif (
        model_id in OPENROUTER_FREE_MODEL_IDS
        or model_id.endswith(OPENROUTER_FREE_SUFFIX)
    ) and (all_zero_price or not pricing):
        free_status = FREE_STATUS_VERIFIED
        in_price_1m = 0.0 if in_price_1m is None else in_price_1m
        out_price_1m = 0.0 if out_price_1m is None else out_price_1m
        req_price = 0.0 if req_price is None else req_price
    else:
        free_status = FREE_STATUS_UNVERIFIED

    supported_params = entry.get("supported_parameters") or []
    supports_structured = isinstance(supported_params, list) and (
        "structured_outputs" in supported_params or "response_format" in supported_params
    )

    arch = entry.get("architecture") if isinstance(entry.get("architecture"), dict) else {}
    input_mods = arch.get("input_modalities")
    modalities = (
        [str(m) for m in input_mods]
        if isinstance(input_mods, list) and input_mods
        else ["text"]
    )

    ctx_len = entry.get("context_length")
    return DiscoveredModelSchema(
        model_id=model_id,
        provider=PROVIDER_NAME_OPENROUTER,
        model_name=model_name,
        free_tier_status=free_status,
        input_price_per_1m=in_price_1m,
        output_price_per_1m=out_price_1m,
        request_price=req_price,
        context_length=int(ctx_len) if isinstance(ctx_len, int) else None,
        modalities=modalities,
        supports_structured_output=bool(supports_structured),
        availability_status=AVAILABILITY_AVAILABLE,
        source_info="openrouter_models_api",
    )


class OpenRouterProvider(BaseLlmProvider):
    """OpenRouter 無料モデル専用アダプタ。"""

    def __init__(
        self,
        api_key: str | None = None,
        enabled: bool | None = None,
        base_url: str | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        """OpenRouterProvider を初期化する。

        Args:
            api_key (str | None): OpenRouter APIキー（省略時は Settings から取得）。
            enabled (bool | None): OpenRouter有効フラグ（省略時は Settings から取得）。
            base_url (str | None): APIベースURL（既定 `https://openrouter.ai/api/v1`）。
            http_client (httpx.Client | None): テスト用に外部注入する `httpx.Client`。
        """
        cfg = get_settings()
        self._api_key = cfg.openrouter_api_key if api_key is None else api_key
        self._enabled = cfg.openrouter_enabled if enabled is None else enabled
        self._base_url = (cfg.openrouter_base_url if base_url is None else base_url).rstrip(
            "/"
        )
        self._http_client = http_client
        self._timeout_seconds = float(cfg.llm_request_timeout_seconds)

    @property
    def provider_name(self) -> str:
        """プロバイダー名 `"openrouter"` を返す。"""
        return PROVIDER_NAME_OPENROUTER

    def is_configured(self) -> bool:
        """OpenRouter が有効化されており、かつ APIキーが設定されているか返す。"""
        return bool(self._enabled and self._api_key and self._api_key.strip())

    def verify_model_is_free(
        self,
        model_id: str,
        catalog_meta: dict[str, Any] | None = None,
    ) -> bool:
        """モデルが `:free` または `openrouter/free` であり、料金が0以下か検証する。"""
        return is_openrouter_model_verified_free(model_id, catalog_meta=catalog_meta)

    def fetch_raw_models_list(self) -> list[dict[str, Any]]:
        """OpenRouter の `/models` エンドポイントから生のモデル辞書一覧を取得する。

        Returns:
            list[dict[str, Any]]: OpenRouter `data` 配列内のモデル辞書リスト。

        Side Effects:
            OpenRouter `/models` へのHTTP GET通信を行う。
        """
        url = f"{self._base_url}/models"
        headers: dict[str, str] = {"Accept": "application/json"}
        if self._api_key and self._api_key.strip():
            headers["Authorization"] = f"Bearer {self._api_key.strip()}"

        close_client = False
        client = self._http_client
        if client is None:
            client = httpx.Client(timeout=self._timeout_seconds)
            close_client = True

        try:
            response = client.get(url, headers=headers)
            response.raise_for_status()
            payload = response.json()
            data_list = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data_list, list):
                return []
            return [item for item in data_list if isinstance(item, dict)]
        except Exception as exc:
            logger.warning(
                "OpenRouter /models の取得に失敗しました: %s",
                mask_sensitive_text(str(exc)),
            )
            return []
        finally:
            if close_client and client is not None:
                client.close()

    def list_available_models(self) -> list[DiscoveredModelSchema]:
        """OpenRouter のモデル一覧を取得し、無料判定結果を付与して返す（指示書 §12.1）。"""
        raw_entries = self.fetch_raw_models_list()
        results: list[DiscoveredModelSchema] = []
        for entry in raw_entries:
            parsed = parse_openrouter_model_entry(entry)
            if parsed is not None:
                results.append(parsed)
        return results

    def _call_api(
        self,
        model_id: str,
        system_prompt: str,
        user_prompt: str,
        response_schema: type[BaseModel],
    ) -> tuple[str, LlmTokenUsageSchema, int | None]:
        """OpenRouter `/chat/completions` を呼び出し、テキストとトークン使用量を返す。"""
        if not self._api_key or not self._api_key.strip():
            raise ProviderHttpError("OPENROUTER_API_KEY が設定されていません。", http_status=401)

        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key.strip()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        request_body: dict[str, Any] = {
            "model": model_id,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": DEFAULT_OPENROUTER_TEMPERATURE,
        }

        close_client = False
        client = self._http_client
        if client is None:
            client = httpx.Client(timeout=self._timeout_seconds)
            close_client = True

        try:
            response = client.post(url, headers=headers, json=request_body)
            status_code = response.status_code

            if status_code >= 400:
                err_detail = response.text
                try:
                    err_json = response.json()
                    if isinstance(err_json, dict) and "error" in err_json:
                        err_obj = err_json["error"]
                        if isinstance(err_obj, dict):
                            err_detail = str(err_obj.get("message") or err_obj)
                        else:
                            err_detail = str(err_obj)
                except Exception:
                    pass
                raise ProviderHttpError(
                    mask_sensitive_text(f"OpenRouter API エラー (HTTP {status_code}): {err_detail}"),
                    http_status=status_code,
                )

            payload = response.json()
            if isinstance(payload, dict) and "error" in payload:
                err_obj = payload["error"]
                err_code = (
                    int(err_obj["code"])
                    if isinstance(err_obj, dict) and isinstance(err_obj.get("code"), int)
                    else 500
                )
                err_msg = (
                    str(err_obj.get("message") or err_obj)
                    if isinstance(err_obj, dict)
                    else str(err_obj)
                )
                raise ProviderHttpError(
                    mask_sensitive_text(f"OpenRouter エラー応答: {err_msg}"),
                    http_status=err_code,
                )

            choices = payload.get("choices") if isinstance(payload, dict) else None
            if not isinstance(choices, list) or not choices:
                raise ProviderHttpError(
                    "OpenRouter 応答に choices が含まれていません。",
                    http_status=status_code,
                )

            first_choice = choices[0] if isinstance(choices[0], dict) else {}
            message_obj = (
                first_choice.get("message")
                if isinstance(first_choice.get("message"), dict)
                else {}
            )
            content = str(message_obj.get("content") or "")
            usage = extract_openrouter_usage(
                payload.get("usage") if isinstance(payload, dict) else None
            )
            return content, usage, status_code
        finally:
            if close_client and client is not None:
                client.close()


def validate_input(data: dict[str, Any]) -> dict[str, Any]:
    """OpenRouter 無料判定テスト用の入力辞書を検証する。

    Args:
        data (dict[str, Any]): `model_id` を含む辞書。

    Returns:
        dict[str, Any]: 検証済み辞書。

    Raises:
        ValueError: `model_id` が無い場合。

    Side Effects:
        なし。

    Examples:
        >>> validate_input({"model_id": "openrouter/free"})["model_id"]
        'openrouter/free'
    """
    if not isinstance(data, dict) or not data.get("model_id"):
        raise ValueError("model_id を含む辞書が必要です。")
    return dict(data)


def execute_logic(validated: dict[str, Any]) -> dict[str, Any]:
    """指定モデルが OpenRouter で無料実行可能かを判定する。"""
    model_id = str(validated["model_id"])
    catalog_meta = validated.get("catalog_meta")
    is_free = is_openrouter_model_verified_free(model_id, catalog_meta=catalog_meta)
    return {
        "model_id": model_id,
        "is_verified_free": is_free,
    }


def format_output(result: dict[str, Any]) -> dict[str, Any]:
    """結果辞書を整形して返す。"""
    return dict(result)


def main(data: dict[str, Any]) -> dict[str, Any]:
    """OpenRouter 無料モデル判定を実行する。"""
    validated = validate_input(data)
    res = execute_logic(validated)
    return format_output(res)
