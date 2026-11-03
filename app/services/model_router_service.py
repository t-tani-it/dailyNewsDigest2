"""無料枠監視・1日呼び出し上限チェック・無料LLMモデル自動切替ルーターサービス。

目的（Why）:
    ユーザーが選択したモード（自動選択 / Gemini選択 / OpenRouter選択）と優先モデルに従いつつ、
    レート制限（429）・無料枠上限・認証エラー・モデル提供終了が発生した際に
    「利用可能な別の無料モデル」へ自動的にフォールバックし、全無料モデルが利用不能な場合は
    有料モデルへ絶対に切り替えず安全側（RSS記事保存＋AI保留）で停止する
    （指示書 §10, §11, §12.2, §12.3, §13, §14）。
仕様（What）:
    - `count_today_api_calls` / `check_daily_call_limit`: 日本時間当日のAPI呼び出し回数監視
    - `build_candidate_models_order`: 選択モード・優先モデル・自動切替設定に基づく無料モデル試行順リスト生成
    - `record_llm_call_log`: 成功・失敗・切替理由を含む `LlmUsageLog` のDB保存
    - `execute_with_free_model_routing`: 無料モデル自動切替付きのLLM呼び出し実行
前提条件・依存関係（Assumption / Dependencies）:
    sqlalchemy>=2.0, pydantic>=2.7, `app.models`, `app.providers`, `app.services.model_catalog_service`
入出力（I/O）:
    入力: SQLAlchemy `Session`, `run_id`, 用途（`classify` / `summarize`）, プロンプト, 検証スキーマ
    出力: 成否・検証済みデータ・使用されたモデル・切替履歴・保留フラグを含む辞書
注意点（Caution）:
    - いかなる障害時も有料モデル（`PAID_COMPARISON_MODEL_IDS` 等）を候補に含めてはならない。
    - `auto_switch_enabled == False` の場合は、失敗時に別モデルへの自動切替を行わない。
今後の拡張ポイント（Future Work）:
    レート制限解除予定時刻（Retry-After）に基づく一時クールダウン復帰制御。
変更履歴（Change Log）:
    2026-11-03 - 初版作成（Step 4-2）
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.model_catalog import (
    AVAILABILITY_AUTH_ERROR,
    AVAILABILITY_AVAILABLE,
    AVAILABILITY_QUOTA_EXHAUSTED,
    AVAILABILITY_RATE_LIMITED,
    AVAILABILITY_UNAVAILABLE,
    ModelCatalog,
)
from app.models.usage_log import LlmUsageLog
from app.models.usage_log import execute_logic as build_usage_log_model
from app.providers.base_provider import BaseLlmProvider
from app.providers.gemini_provider import PROVIDER_NAME_GEMINI, GeminiProvider
from app.providers.openrouter_provider import (
    PROVIDER_NAME_OPENROUTER,
    OpenRouterProvider,
)
from app.repositories.setting_repo import SettingRepository
from app.schemas.llm_schema import (
    LLM_ERROR_AUTH,
    LLM_ERROR_MODEL_UNAVAILABLE,
    LLM_ERROR_QUOTA_EXHAUSTED,
    LLM_ERROR_RATE_LIMIT,
    PAID_COMPARISON_MODEL_IDS,
    LlmProviderCallResultSchema,
)
from app.services.metrics_service import current_jst_date
from app.services.model_catalog_service import (
    list_catalog_models,
    update_model_availability,
)
from app.services.rss_service import get_jst_day_utc_bounds

# =============================================================================
# 設定値ブロック（エラー種別→可用性ステータスマッピング）
# =============================================================================

ERROR_TO_AVAILABILITY_MAP: dict[str, str] = {
    LLM_ERROR_RATE_LIMIT: AVAILABILITY_RATE_LIMITED,
    LLM_ERROR_QUOTA_EXHAUSTED: AVAILABILITY_QUOTA_EXHAUSTED,
    LLM_ERROR_AUTH: AVAILABILITY_AUTH_ERROR,
    LLM_ERROR_MODEL_UNAVAILABLE: AVAILABILITY_UNAVAILABLE,
}

logger = get_logger(__name__)


def count_today_api_calls(
    session: Session,
    target_date: date | None = None,
) -> int:
    """日本時間の当日（0:00〜24:00）に実行されたLLM API呼び出し回数を集計する（指示書 §12.2）。

    Args:
        session (Session): SQLAlchemy セッション。
        target_date (date | None): 集計対象日（JST）。省略時は本日。

    Returns:
        int: 当日のAPI呼び出し回数。

    Side Effects:
        なし。
    """
    jst_date = target_date or current_jst_date()
    start_utc, end_utc = get_jst_day_utc_bounds(jst_date)
    stmt = select(func.count(LlmUsageLog.id)).where(
        LlmUsageLog.called_at >= start_utc,
        LlmUsageLog.called_at < end_utc,
    )
    return int(session.scalar(stmt) or 0)


def check_daily_call_limit(
    session: Session,
    daily_limit: int | None = None,
    target_date: date | None = None,
) -> dict[str, Any]:
    """1日あたりのAPI呼び出し回数安全上限に達していないか判定する（指示書 §12.2）。

    Args:
        session (Session): SQLAlchemy セッション。
        daily_limit (int | None): 上限回数（省略時は `UserSetting.daily_api_call_limit`）。
        target_date (date | None): 集計対象日（JST）。

    Returns:
        dict[str, Any]: `can_call` (bool), `today_calls` (int), `daily_limit` (int), `remaining_calls` (int)。

    Side Effects:
        なし。
    """
    if daily_limit is None:
        setting = SettingRepository(session).get_or_create()
        limit_val = int(setting.daily_api_call_limit)
    else:
        limit_val = int(daily_limit)

    today_calls = count_today_api_calls(session, target_date=target_date)
    remaining = max(0, limit_val - today_calls)
    return {
        "can_call": today_calls < limit_val,
        "today_calls": today_calls,
        "daily_limit": limit_val,
        "remaining_calls": remaining,
    }


def build_candidate_models_order(
    session: Session,
    selection_mode: str = "auto",
    preferred_model_id: str | None = None,
    auto_switch_enabled: bool = True,
    gemini_configured: bool = True,
    openrouter_configured: bool = True,
) -> list[ModelCatalog]:
    """選択モード・優先モデル・自動切替設定に基づき、試行する無料モデルの順序リストを返す（指示書 §12.3, §13）。

    解説：
        フォールバック順序：
        1. `model_catalog` のうち `is_executable_free_model == True`（無料確認済み・料金0・available）
           かつプロバイダーのAPIキーが設定済みのモデルのみを母集団とする。
        2. 有料比較モデル（`PAID_COMPARISON_MODEL_IDS`）は絶対に候補へ入れない。
        3. ユーザーの優先モデル（`preferred_model_id`）が母集団にあれば先頭に置く。
        4. 続いて選択モード（`gemini` / `openrouter` / `auto`）の同一プロバイダー内無料モデルを並べ、
           さらに `auto_switch_enabled == True` なら別プロバイダーの無料モデルを後続に並べる。
        5. `auto_switch_enabled == False` の場合は先頭1モデルのみを返し、勝手に別モデルへ切り替えない。

    Args:
        session (Session): SQLAlchemy セッション。
        selection_mode (str): `"auto"`, `"gemini"`, `"openrouter"`。
        preferred_model_id (str | None): ユーザーが明示選択したモデルID。
        auto_switch_enabled (bool): 自動モデル切替のON/OFF。
        gemini_configured (bool): Gemini APIキーおよびFree Tier確認が完了しているか。
        openrouter_configured (bool): OpenRouter APIキーが設定されているか。

    Returns:
        list[ModelCatalog]: 試行順に整列された無料確認済みモデルリスト。

    Side Effects:
        カタログが空の場合に初期シードを登録する。
    """
    executable_models = list_catalog_models(session, executable_only=True)

    configured_providers: set[str] = set()
    if gemini_configured:
        configured_providers.add(PROVIDER_NAME_GEMINI)
    if openrouter_configured:
        configured_providers.add(PROVIDER_NAME_OPENROUTER)

    valid_pool = [
        m
        for m in executable_models
        if m.provider in configured_providers
        and m.model_id not in PAID_COMPARISON_MODEL_IDS
        and m.is_executable_free_model
    ]
    if not valid_pool:
        return []

    mode = (selection_mode or "auto").strip().lower()
    pref_id = (preferred_model_id or "").strip() or None

    # 自動切替がOFFで特定の優先モデルが指定されている場合、そのモデルのみを試行対象とする
    if not auto_switch_enabled and pref_id:
        matched_pref = [
            m
            for m in valid_pool
            if m.model_id == pref_id and (mode == "auto" or m.provider == mode)
        ]
        return matched_pref[:1]

    gemini_models = [m for m in valid_pool if m.provider == PROVIDER_NAME_GEMINI]
    # OpenRouter は openrouter/free を先頭に、続けてその他の :free モデルを並べる
    openrouter_models = sorted(
        [m for m in valid_pool if m.provider == PROVIDER_NAME_OPENROUTER],
        key=lambda x: (0 if x.model_id == "openrouter/free" else 1, x.model_id),
    )

    ordered: list[ModelCatalog] = []

    if pref_id:
        for m in valid_pool:
            if m.model_id == pref_id and (mode == "auto" or m.provider == mode):
                ordered.append(m)
                break

    if mode == PROVIDER_NAME_GEMINI:
        primary_group = gemini_models
        secondary_group = openrouter_models if auto_switch_enabled else []
    elif mode == PROVIDER_NAME_OPENROUTER:
        primary_group = openrouter_models
        secondary_group = gemini_models if auto_switch_enabled else []
    else:
        # auto モード: Gemini Free Tier → OpenRouter Free の順で無料枠を活用する
        primary_group = gemini_models + openrouter_models
        secondary_group = []

    for m in primary_group + secondary_group:
        if m not in ordered:
            ordered.append(m)

    if not auto_switch_enabled:
        return ordered[:1]

    return ordered


def record_llm_call_log(
    session: Session,
    run_id: str,
    call_result: LlmProviderCallResultSchema,
    target_article_count: int = 0,
    switched_from_model: str | None = None,
    switch_reason: str | None = None,
) -> LlmUsageLog:
    """1回のLLM API呼び出し結果（成功・失敗・トークン使用量・切替理由）をDBへ記録する（指示書 §14, §19）。

    Args:
        session (Session): SQLAlchemy セッション。
        run_id (str): 実行ID（`news_runs.run_id`）。
        call_result (LlmProviderCallResultSchema): Provider呼び出し結果。
        target_article_count (int): この呼び出しで処理対象とした記事数。
        switched_from_model (str | None): 自動切替元のモデルID。
        switch_reason (str | None): 自動切替の理由。

    Returns:
        LlmUsageLog: 保存された利用履歴レコード。

    Side Effects:
        `llm_usage_logs` テーブルへ INSERT し commit する。
    """
    usage = call_result.usage
    log_data: dict[str, Any] = {
        "run_id": run_id,
        "purpose": call_result.purpose,
        "provider": call_result.provider,
        "model_id": call_result.model_id,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
        "total_tokens": usage.total_tokens,
        "reasoning_included_in_output": usage.reasoning_included_in_output,
        "usage_status": usage.usage_status if call_result.success else "failed",
        "http_status": call_result.http_status,
        "error_message": call_result.error_message,
        "switched_from_model": switched_from_model,
        "switch_reason": switch_reason,
        "target_article_count": int(target_article_count),
    }
    log_obj = build_usage_log_model(log_data)
    session.add(log_obj)
    session.commit()
    session.refresh(log_obj)
    return log_obj


def execute_with_free_model_routing(
    session: Session,
    run_id: str,
    purpose: str,
    system_prompt: str,
    user_prompt: str,
    response_schema: type[BaseModel],
    target_article_count: int = 0,
    gemini_provider: GeminiProvider | None = None,
    openrouter_provider: OpenRouterProvider | None = None,
    selection_mode_override: str | None = None,
    preferred_model_override: str | None = None,
    auto_switch_override: bool | None = None,
) -> dict[str, Any]:
    """無料枠監視と自動モデル切替を行いながらLLM構造化出力を実行する（指示書 §12.2, §12.3, §14）。

    解説：
        1. ユーザー設定で `llm_enabled == False` なら通信せずスキップする。
        2. 当日のAPI呼び出し回数が安全上限に達していれば、通信せず `ai_pending=True` で停止する。
        3. `build_candidate_models_order` で無料確認済みモデルの試行順リストを作る。
        4. 順番に呼び出し、成功したら即座に検証済みデータとトークン使用量を返す。
        5. レート制限等で失敗した場合は、そのモデルの状態（`rate_limited` 等）をDBに記録し、
           `switched_from_model` と `switch_reason` を引き継いで次の無料モデルへ自動切替する。
        6. 全無料モデルが失敗した場合は有料モデルへ絶対に切り替えず、`ai_pending=True` を返す。

    Args:
        session (Session): SQLAlchemy セッション。
        run_id (str): 実行ID。
        purpose (str): `"classify"` または `"summarize"`。
        system_prompt (str): システムプロンプト。
        user_prompt (str): ユーザープロンプト。
        response_schema (type[BaseModel]): 検証用Pydanticスキーマ。
        target_article_count (int): 対象記事数。
        gemini_provider (GeminiProvider | None): Gemini アダプタ。
        openrouter_provider (OpenRouterProvider | None): OpenRouter アダプタ。
        selection_mode_override (str | None): 選択モードの一時上書き。
        preferred_model_override (str | None): 優先モデルIDの一時上書き。
        auto_switch_override (bool | None): 自動切替の一時上書き。

    Returns:
        dict[str, Any]: `success`, `ai_pending`, `provider`, `model_id`, `parsed_data`, `usage`, `reason` 等を含む辞書。

    Side Effects:
        外部LLM API通信、`llm_usage_logs` への記録、`model_catalog` の状態更新を行う。
    """
    setting = SettingRepository(session).get_or_create()
    if not setting.llm_enabled:
        return {
            "success": False,
            "skipped": True,
            "ai_pending": False,
            "reason": "llm_disabled",
            "parsed_data": None,
            "provider": None,
            "model_id": None,
        }

    limit_check = check_daily_call_limit(session, daily_limit=setting.daily_api_call_limit)
    if not limit_check["can_call"]:
        logger.warning(
            "1日あたりのLLM API呼び出し上限（%d回）に達したためAI処理を保留します。",
            limit_check["daily_limit"],
        )
        return {
            "success": False,
            "skipped": False,
            "ai_pending": True,
            "reason": "daily_call_limit_reached",
            "parsed_data": None,
            "provider": None,
            "model_id": None,
        }

    gem = gemini_provider or GeminiProvider()
    orc = openrouter_provider or OpenRouterProvider()
    providers_map: dict[str, BaseLlmProvider] = {
        PROVIDER_NAME_GEMINI: gem,
        PROVIDER_NAME_OPENROUTER: orc,
    }

    mode = (
        selection_mode_override
        if selection_mode_override is not None
        else setting.selection_mode
    )
    pref_model = (
        preferred_model_override
        if preferred_model_override is not None
        else setting.preferred_model_id
    )
    auto_switch = (
        auto_switch_override
        if auto_switch_override is not None
        else setting.auto_switch_enabled
    )

    candidates = build_candidate_models_order(
        session=session,
        selection_mode=mode,
        preferred_model_id=pref_model,
        auto_switch_enabled=auto_switch,
        gemini_configured=gem.is_configured(),
        openrouter_configured=orc.is_configured(),
    )

    if not candidates:
        return {
            "success": False,
            "skipped": False,
            "ai_pending": True,
            "reason": "no_available_free_models",
            "parsed_data": None,
            "provider": None,
            "model_id": None,
        }

    switched_from: str | None = None
    switch_reason: str | None = None
    last_error_msg: str | None = None

    for candidate in candidates:
        # ループ中に1日上限へ達した場合も安全に停止する
        if not check_daily_call_limit(session, daily_limit=setting.daily_api_call_limit)["can_call"]:
            return {
                "success": False,
                "skipped": False,
                "ai_pending": True,
                "reason": "daily_call_limit_reached",
                "parsed_data": None,
                "provider": None,
                "model_id": None,
                "last_error": last_error_msg,
            }

        provider = providers_map.get(candidate.provider)
        if provider is None:
            continue

        catalog_meta = {
            "free_tier_status": candidate.free_tier_status,
            "input_price_per_1m": candidate.input_price_per_1m,
            "output_price_per_1m": candidate.output_price_per_1m,
            "request_price": candidate.request_price,
        }

        call_res = provider.generate_structured(
            model_id=candidate.model_id,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_schema=response_schema,
            purpose=purpose,
            catalog_meta=catalog_meta,
        )

        record_llm_call_log(
            session=session,
            run_id=run_id,
            call_result=call_res,
            target_article_count=target_article_count,
            switched_from_model=switched_from,
            switch_reason=switch_reason,
        )

        if call_res.success:
            if candidate.availability_status != AVAILABILITY_AVAILABLE:
                update_model_availability(
                    session, candidate.model_id, AVAILABILITY_AVAILABLE
                )
            return {
                "success": True,
                "skipped": False,
                "ai_pending": False,
                "reason": None,
                "provider": call_res.provider,
                "model_id": call_res.model_id,
                "parsed_data": call_res.parsed_data,
                "usage": call_res.usage.model_dump(),
                "switched_from_model": switched_from,
                "switch_reason": switch_reason,
            }

        last_error_msg = call_res.error_message
        new_avail = ERROR_TO_AVAILABILITY_MAP.get(call_res.error_type or "")
        if new_avail:
            update_model_availability(session, candidate.model_id, new_avail)

        switched_from = candidate.model_id
        switch_reason = call_res.error_type or "call_failed"
        logger.info(
            "無料モデル %s (%s) が失敗したため次候補を探します (reason=%s)",
            candidate.model_id,
            candidate.provider,
            switch_reason,
        )

    return {
        "success": False,
        "skipped": False,
        "ai_pending": True,
        "reason": "all_free_models_failed",
        "parsed_data": None,
        "provider": None,
        "model_id": None,
        "switched_from_model": switched_from,
        "switch_reason": switch_reason,
        "last_error": last_error_msg,
    }


def validate_input(data: dict[str, Any]) -> dict[str, Any]:
    """ルーターサービスの入力辞書を検証する。"""
    if not isinstance(data, dict) or not isinstance(data.get("session"), Session):
        raise ValueError("有効な SQLAlchemy Session が必要です。")
    return dict(data)


def execute_logic(validated: dict[str, Any]) -> dict[str, Any]:
    """候補モデル順の算出または上限チェックを実行する。"""
    session: Session = validated["session"]
    action = str(validated.get("action", "candidates"))
    if action == "check_limit":
        return check_daily_call_limit(
            session=session,
            daily_limit=validated.get("daily_limit"),
        )
    candidates = build_candidate_models_order(
        session=session,
        selection_mode=str(validated.get("selection_mode", "auto")),
        preferred_model_id=validated.get("preferred_model_id"),
        auto_switch_enabled=bool(validated.get("auto_switch_enabled", True)),
        gemini_configured=bool(validated.get("gemini_configured", True)),
        openrouter_configured=bool(validated.get("openrouter_configured", True)),
    )
    return {
        "candidate_model_ids": [m.model_id for m in candidates],
        "count": len(candidates),
    }


def format_output(result: dict[str, Any]) -> dict[str, Any]:
    """結果辞書を整形して返す。"""
    return dict(result)


def main(data: dict[str, Any]) -> dict[str, Any]:
    """モデルルーターの一連の処理を実行する。"""
    validated = validate_input(data)
    res = execute_logic(validated)
    return format_output(res)
