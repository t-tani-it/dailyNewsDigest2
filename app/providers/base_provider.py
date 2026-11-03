"""LLM Provider（Adapter）層の抽象基底クラスと無料利用ポリシー強制ガード。

目的（Why）:
    Service層からGemini APIやOpenRouter APIの通信詳細を隠蔽し（Adapterパターン）、
    「無料利用確認済みモデルのみ実行する」「有料比較モデルは絶対に呼び出さない」
    「検証エラー時の再試行回数を制限する（無限リトライ禁止）」を全Provider共通で強制する
    （指示書 §4.1, §4.2, §10, §11, §12.3, §16）。
仕様（What）:
    - `FreePolicyViolationError`: 無料利用条件を満たさないモデル呼び出しを遮断する例外
    - `BaseLlmProvider`: 無料モデル検証・構造化JSON検証・回数制限リトライ・トークン抽出の共通基底クラス
    - `classify_provider_exception`: HTTPステータスや例外文言から自動切替用のエラー種別を判定する関数
前提条件・依存関係（Assumption / Dependencies）:
    pydantic>=2.7, `app.core.config`, `app.core.logging`, `app.schemas.llm_schema`
入出力（I/O）:
    入力: モデルID、システムプロンプト、ユーザープロンプト、検証先Pydanticスキーマクラス
    出力: `LlmProviderCallResultSchema`（成否、検証済みデータ、トークン使用量、エラー種別）
注意点（Caution）:
    - いかなる場合も `PAID_COMPARISON_MODEL_IDS` に含まれるモデルへリクエストを送信してはならない。
    - APIキーや認証ヘッダーを例外メッセージ・ログに含めてはならない（`mask_sensitive_text` 必須）。
今後の拡張ポイント（Future Work）:
    ローカルLLM（Ollama等）用Providerの追加。
変更履歴（Change Log）:
    2026-11-03 - 初版作成（Step 4-1）
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel

from app.core.config import ALLOW_PAID_LLM_EXECUTION, get_settings
from app.core.logging import get_logger, mask_sensitive_text
from app.schemas.llm_schema import (
    LLM_ERROR_AUTH,
    LLM_ERROR_MODEL_UNAVAILABLE,
    LLM_ERROR_NETWORK,
    LLM_ERROR_POLICY_VIOLATION,
    LLM_ERROR_QUOTA_EXHAUSTED,
    LLM_ERROR_RATE_LIMIT,
    LLM_ERROR_VALIDATION,
    PAID_COMPARISON_MODEL_IDS,
    DiscoveredModelSchema,
    LlmProviderCallResultSchema,
    LlmTokenUsageSchema,
    parse_and_validate_llm_json,
)

# =============================================================================
# 設定値ブロック（リトライ上限・エラー分類キーワード）
# =============================================================================

# 無限リトライを防止するシステム絶対上限（指示書 §4.2）
MAX_ALLOWED_LLM_RETRIES: int = 5

logger = get_logger(__name__)


class FreePolicyViolationError(ValueError):
    """無料利用ポリシーに違反するモデルの実行要求を遮断した際に送出される例外。"""


class ProviderHttpError(RuntimeError):
    """HTTPステータスコードを伴うProvider通信エラー。"""

    def __init__(self, message: str, http_status: int | None = None) -> None:
        """エラーメッセージとHTTPステータスコードを保持する。"""
        super().__init__(message)
        self.http_status = http_status


def classify_provider_exception(
    exc: Exception,
    http_status: int | None = None,
) -> tuple[str, int | None, str]:
    """Provider呼び出し時の例外を自動モデル切替用のエラー種別へ分類する（指示書 §12.3）。

    解説：
        APIが失敗した理由によって次の行動が変わる。
        - レート制限（429）や無料枠上限（quota）：別の無料モデルへ自動切替する。
        - ポリシー違反（有料モデル指定）：即座に遮断しリトライしない。
        - JSON検証エラー：最大 `max_retries` 回だけ再試行し、それでも駄目なら別モデルへ切替する。

    Args:
        exc (Exception): 発生した例外インスタンス。
        http_status (int | None): 判明している場合のHTTPステータスコード。

    Returns:
        tuple[str, int | None, str]: `(error_type, http_status, masked_error_message)`。

    Side Effects:
        なし。

    Examples:
        >>> err_type, status, _ = classify_provider_exception(ProviderHttpError("Too Many Requests", 429))
        >>> (err_type, status)
        ('rate_limit', 429)
    """
    if isinstance(exc, FreePolicyViolationError):
        return LLM_ERROR_POLICY_VIOLATION, http_status, mask_sensitive_text(str(exc))

    detected_status = http_status
    if detected_status is None and isinstance(exc, ProviderHttpError):
        detected_status = exc.http_status
    if detected_status is None:
        status_attr = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        if isinstance(status_attr, int):
            detected_status = status_attr

    safe_msg = mask_sensitive_text(str(exc))
    lower_msg = safe_msg.lower()

    if detected_status == 402 or "credit" in lower_msg or "billing" in lower_msg:
        return LLM_ERROR_QUOTA_EXHAUSTED, detected_status, safe_msg

    if "quota" in lower_msg or "resource_exhausted" in lower_msg or "daily limit" in lower_msg:
        return LLM_ERROR_QUOTA_EXHAUSTED, detected_status or 429, safe_msg

    if detected_status == 429 or "rate limit" in lower_msg or "too many requests" in lower_msg:
        return LLM_ERROR_RATE_LIMIT, detected_status or 429, safe_msg

    if (
        detected_status in (401, 403)
        or "api key" in lower_msg
        or "api_key" in lower_msg
        or "unauthorized" in lower_msg
        or "permission_denied" in lower_msg
    ):
        return LLM_ERROR_AUTH, detected_status, safe_msg

    if (
        detected_status in (404, 410, 503)
        or "not found" in lower_msg
        or "no endpoints found" in lower_msg
        or "model_not_found" in lower_msg
        or "unavailable" in lower_msg
    ):
        return LLM_ERROR_MODEL_UNAVAILABLE, detected_status, safe_msg

    return LLM_ERROR_NETWORK, detected_status, safe_msg


class BaseLlmProvider(ABC):
    """Gemini API と OpenRouter API の共通インターフェースを定義する抽象基底クラス。"""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """プロバイダー識別子（`"gemini"` または `"openrouter"`）を返す。"""

    @abstractmethod
    def is_configured(self) -> bool:
        """APIキーおよび無料利用前提条件が設定済みかを返す。"""

    @abstractmethod
    def verify_model_is_free(
        self,
        model_id: str,
        catalog_meta: dict[str, Any] | None = None,
    ) -> bool:
        """指定モデルが当該プロバイダーで無料実行可能と確認されているか検証する。"""

    @abstractmethod
    def list_available_models(self) -> list[DiscoveredModelSchema]:
        """プロバイダーのモデル一覧を取得し、無料判定結果を付与して返す。"""

    @abstractmethod
    def _call_api(
        self,
        model_id: str,
        system_prompt: str,
        user_prompt: str,
        response_schema: type[BaseModel],
    ) -> tuple[str, LlmTokenUsageSchema, int | None]:
        """プロバイダー固有のAPI通信を実行し `(raw_text, usage, http_status)` を返す。"""

    def ensure_free_execution_allowed(
        self,
        model_id: str,
        catalog_meta: dict[str, Any] | None = None,
    ) -> None:
        """API呼び出し直前に「無料利用ポリシー」を満たしているか強制検証する（指示書 §10, §11, §16）。

        Args:
            model_id (str): 呼び出し対象モデルID。
            catalog_meta (dict[str, Any] | None): カタログ上の料金・無料確認情報（任意）。

        Raises:
            FreePolicyViolationError: 有料比較モデル、料金不明モデル、または未設定状態の場合。

        Side Effects:
            なし。
        """
        if ALLOW_PAID_LLM_EXECUTION:
            raise FreePolicyViolationError(
                "ALLOW_PAID_LLM_EXECUTION が不正に有効化されています。実行を停止します。"
            )

        clean_model_id = (model_id or "").strip()
        if not clean_model_id:
            raise FreePolicyViolationError("モデルIDが空のため実行できません。")

        if clean_model_id in PAID_COMPARISON_MODEL_IDS:
            raise FreePolicyViolationError(
                f"モデル '{clean_model_id}' は参考料金比較専用の有料モデルであり、API呼び出しは禁止されています。"
            )

        if not self.is_configured():
            raise FreePolicyViolationError(
                f"プロバイダー '{self.provider_name}' はAPIキー未設定または無料枠未確認のため実行できません。"
            )

        if not self.verify_model_is_free(clean_model_id, catalog_meta=catalog_meta):
            raise FreePolicyViolationError(
                f"モデル '{clean_model_id}' ({self.provider_name}) は無料利用条件が確認できないため実行を停止しました。"
            )

    def generate_structured(
        self,
        model_id: str,
        system_prompt: str,
        user_prompt: str,
        response_schema: type[BaseModel],
        purpose: str = "classify",
        catalog_meta: dict[str, Any] | None = None,
        max_retries: int | None = None,
    ) -> LlmProviderCallResultSchema:
        """無料ポリシー検証のうえでLLMを呼び出し、共通Pydanticスキーマで出力を検証する（指示書 §4.2, §14）。

        解説：
            1. 通信前に `ensure_free_execution_allowed` を実行し、有料モデルや未確認モデルを確実に遮断する。
            2. `_call_api` で応答テキストとトークン使用量を取得する。
            3. `parse_and_validate_llm_json` で共通スキーマへ変換する。
            4. もしLLMが壊れたJSONを返した場合は、無限ループにならないよう最大 `retry_limit` 回だけ再試行する。
               レート制限やクォータ上限時は同一モデルで再試行せず即座に返し、別モデルへの自動切替に委ねる。

        Args:
            model_id (str): 実行する無料モデルID。
            system_prompt (str): システム指示プロンプト。
            user_prompt (str): 記事データを含むユーザープロンプト。
            response_schema (type[BaseModel]): 検証対象のPydanticモデルクラス。
            purpose (str): `"classify"` または `"summarize"`。
            catalog_meta (dict[str, Any] | None): カタログメタデータ。
            max_retries (int | None): JSON検証失敗時の最大再試行回数（省略時は設定値）。

        Returns:
            LlmProviderCallResultSchema: 成否・検証済みデータ・トークン使用量を含む結果。

        Side Effects:
            外部LLM APIへのHTTP通信を行う。
        """
        try:
            self.ensure_free_execution_allowed(model_id, catalog_meta=catalog_meta)
        except FreePolicyViolationError as exc:
            err_type, status, safe_msg = classify_provider_exception(exc)
            logger.warning(
                "LLM無料ポリシーにより実行を遮断しました (%s / %s): %s",
                self.provider_name,
                model_id,
                safe_msg,
            )
            return LlmProviderCallResultSchema(
                success=False,
                provider=self.provider_name,
                model_id=model_id,
                purpose=purpose,
                error_type=err_type,
                error_message=safe_msg,
                http_status=status,
                attempt_count=0,
            )

        settings = get_settings()
        configured_retries = (
            settings.llm_max_retries if max_retries is None else int(max_retries)
        )
        retry_limit = max(0, min(configured_retries, MAX_ALLOWED_LLM_RETRIES))
        max_attempts = 1 + retry_limit

        last_usage = LlmTokenUsageSchema()
        last_raw_text = ""
        last_status: int | None = None
        last_err_msg = ""

        for attempt in range(1, max_attempts + 1):
            try:
                raw_text, usage, http_status = self._call_api(
                    model_id=model_id,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    response_schema=response_schema,
                )
                last_raw_text = raw_text
                last_usage = usage
                last_status = http_status
            except Exception as api_exc:
                err_type, status, safe_msg = classify_provider_exception(api_exc)
                logger.warning(
                    "LLM API通信エラー (%s / %s, attempt=%d): [%s] %s",
                    self.provider_name,
                    model_id,
                    attempt,
                    err_type,
                    safe_msg,
                )
                return LlmProviderCallResultSchema(
                    success=False,
                    provider=self.provider_name,
                    model_id=model_id,
                    purpose=purpose,
                    raw_text=last_raw_text,
                    usage=last_usage,
                    http_status=status,
                    error_type=err_type,
                    error_message=safe_msg,
                    attempt_count=attempt,
                )

            try:
                validated_model = parse_and_validate_llm_json(raw_text, response_schema)
                return LlmProviderCallResultSchema(
                    success=True,
                    provider=self.provider_name,
                    model_id=model_id,
                    purpose=purpose,
                    parsed_data=validated_model.model_dump(),
                    raw_text=raw_text,
                    usage=usage,
                    http_status=http_status or 200,
                    attempt_count=attempt,
                )
            except ValueError as val_exc:
                last_err_msg = mask_sensitive_text(str(val_exc))
                logger.warning(
                    "LLM出力スキーマ検証失敗 (%s / %s, attempt=%d/%d): %s",
                    self.provider_name,
                    model_id,
                    attempt,
                    max_attempts,
                    last_err_msg,
                )

        return LlmProviderCallResultSchema(
            success=False,
            provider=self.provider_name,
            model_id=model_id,
            purpose=purpose,
            raw_text=last_raw_text,
            usage=last_usage,
            http_status=last_status,
            error_type=LLM_ERROR_VALIDATION,
            error_message=last_err_msg or "LLM出力のJSONスキーマ検証に失敗しました。",
            attempt_count=max_attempts,
        )


def validate_input(data: dict[str, Any]) -> dict[str, Any]:
    """基底Provider検証用の入力辞書をチェックする。

    Args:
        data (dict[str, Any]): `error_message` 等を含む辞書。

    Returns:
        dict[str, Any]: 検証済み辞書。

    Side Effects:
        なし。

    Examples:
        >>> validate_input({"error_message": "Rate limit exceeded", "http_status": 429})["http_status"]
        429
    """
    if not isinstance(data, dict):
        raise ValueError("data は dict である必要があります。")
    return dict(data)


def execute_logic(validated: dict[str, Any]) -> dict[str, Any]:
    """例外メッセージとHTTPステータスからエラー種別を判定する。

    Args:
        validated (dict[str, Any]): `validate_input` 通過後の辞書。

    Returns:
        dict[str, Any]: `error_type`, `http_status`, `error_message` を含む辞書。

    Side Effects:
        なし。

    Examples:
        >>> execute_logic({"error_message": "Rate limit", "http_status": 429})["error_type"]
        'rate_limit'
    """
    msg = str(validated.get("error_message", ""))
    status = validated.get("http_status")
    err_type, detected_status, safe_msg = classify_provider_exception(
        ProviderHttpError(msg, status), http_status=status
    )
    return {
        "error_type": err_type,
        "http_status": detected_status,
        "error_message": safe_msg,
    }


def format_output(result: dict[str, Any]) -> dict[str, Any]:
    """結果辞書を整形して返す。"""
    return dict(result)


def main(data: dict[str, Any]) -> dict[str, Any]:
    """例外分類の一連の処理を実行する。"""
    validated = validate_input(data)
    res = execute_logic(validated)
    return format_output(res)
