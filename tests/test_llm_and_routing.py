"""Phase 3 & Phase 4 (Step 4-1〜5-1): LLM無料ポリシー・自動モデル切替・分類/要約・再利用・保留再開のテスト。

目的（Why）:
    「無料確認済みモデルのみ実行する」「有料3モデルは絶対に呼ばない」
    「レート制限・無料枠上限時に別無料モデルへ自動切替する」
    「全無料モデル失敗時もRSS閲覧を維持し ai_pending として後から再開できる」
    「カテゴリ変更時に既存分析を再利用し新規トークン消費を0にする」ことを自動検証する。
仕様（What）:
    - 共通Pydanticスキーマ検証（未知カテゴリの unclassified 化、情報不足時の「概要情報不足」統一）
    - Gemini / OpenRouter Provider の無料ガード・有料モデル遮断・リトライ回数制限
    - モデルカタログ自動発見・許可リスト照合・自動切替ルーター・1日上限監視
    - AI分類・AI要約・カテゴリ再選別（再利用）・`ai_pending` 保留からの再開
前提条件・依存関係（Assumption / Dependencies）:
    pytest, httpx.MockTransport, `app.providers`, `app.services`, `app.schemas.llm_schema`
入出力（I/O）:
    テスト関数ごとに assert で合否を判定する。
注意点（Caution）:
    外部APIへの実通信は一切行わず、モッククライアントまたはモックProviderを注入する。
今後の拡張ポイント（Future Work）:
    複数バッチにまたがる大量記事の並列分類テスト。
変更履歴（Change Log）:
    2026-11-03 - 初版作成（Step 4-5 / Step 5-1）
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.models.analysis import INSUFFICIENT_INFO_TEXT
from app.models.model_catalog import (
    AVAILABILITY_RATE_LIMITED,
    FREE_STATUS_PAID_OR_CONDITIONAL,
    FREE_STATUS_UNVERIFIED,
    FREE_STATUS_VERIFIED,
    ModelCatalog,
)
from app.models.run import RUN_STATUS_AI_PENDING, RUN_STATUS_COMPLETED
from app.providers.base_provider import ProviderHttpError
from app.providers.gemini_provider import GeminiProvider
from app.providers.openrouter_provider import OpenRouterProvider
from app.repositories.run_repo import RunRepository
from app.repositories.setting_repo import SettingRepository
from app.schemas.llm_schema import (
     LLM_ERROR_POLICY_VIOLATION,
    LLM_ERROR_RATE_LIMIT,
    LLM_ERROR_VALIDATION,
    PAID_COMPARISON_MODEL_IDS,
    ArticleClassificationBatchResponseSchema,
    ArticleSummaryBatchResponseSchema,
    parse_and_validate_llm_json,
)
from app.services import digest_orchestrator
from app.services.model_catalog_service import (
    refresh_model_catalog,
    seed_default_models_and_prices,
)
from app.services.model_router_service import (
    build_candidate_models_order,
    execute_with_free_model_routing,
)

# =============================================================================
# 設定値ブロック（テスト用モックヘルパー）
# =============================================================================

BASE_PUB_UTC = datetime(2026, 11, 2, 20, 0, tzinfo=UTC)


class FakeGenaiClient:
    """`google.genai.Client` 互換のテスト用フェイククライアント。"""

    def __init__(
        self,
        response_texts: list[str] | None = None,
        models_list: list[Any] | None = None,
        raise_exc: Exception | None = None,
        prompt_tokens: int | None = 120,
        candidates_tokens: int | None = 45,
        thoughts_tokens: int | None = None,
    ) -> None:
        """応答テキスト列・モデル一覧・送出例外を設定する。"""
        self._texts = list(response_texts or ['{"items": []}'])
        self._models_list = list(models_list or [])
        self._raise_exc = raise_exc
        self._prompt_tokens = prompt_tokens
        self._candidates_tokens = candidates_tokens
        self._thoughts_tokens = thoughts_tokens
        self.call_count = 0
        self.models = self

    def list(self) -> list[Any]:
        """`client.models.list()` のモック実装。"""
        return self._models_list

    def generate_content(self, model: str, contents: str, config: Any) -> Any:
        """`client.models.generate_content()` のモック実装。"""
        self.call_count += 1
        if self._raise_exc is not None:
            raise self._raise_exc
        idx = min(self.call_count - 1, len(self._texts) - 1)
        text = self._texts[idx]
        total = (
            (self._prompt_tokens + self._candidates_tokens + (self._thoughts_tokens or 0))
            if self._prompt_tokens is not None and self._candidates_tokens is not None
            else None
        )
        usage_meta = SimpleNamespace(
            prompt_token_count=self._prompt_tokens,
            candidates_token_count=self._candidates_tokens,
            thoughts_token_count=self._thoughts_tokens,
            total_token_count=total,
        )
        return SimpleNamespace(text=text, usage_metadata=usage_meta)


def _make_openrouter_mock_client(
    handler_fn: Any,
) -> httpx.Client:
    """OpenRouter 通信差し替え用の `httpx.Client` を返す。"""
    return httpx.Client(transport=httpx.MockTransport(handler_fn))


# -----------------------------------------------------------------------------
# 1. 共通Pydanticスキーマ・JSON抽出検証
# -----------------------------------------------------------------------------


def test_parse_and_validate_llm_json_handles_code_fence_and_unknown_category() -> None:
    """コードフェンス付きJSONを抽出し、未知カテゴリを unclassified へ安全変換することを確認する。"""
    raw = """以下が結果です：
```json
[
  {"article_id": 1, "category": "ai_llm", "importance_score": 120, "is_excluded": false},
  {"article_id": 2, "category": "mystery_unknown_cat", "importance_score": -10, "is_excluded": false}
]
```
"""
    parsed = parse_and_validate_llm_json(raw, ArticleClassificationBatchResponseSchema)

    assert len(parsed.items) == 2
    assert parsed.items[0].category == "ai_llm"
    assert parsed.items[0].importance_score == 100
    assert parsed.items[1].category == "unclassified"
    assert parsed.items[1].importance_score == 0


def test_summary_schema_enforces_insufficient_info_without_guessing() -> None:
    """情報不足フラグまたは空要約時に推測補完せず『概要情報不足』へ統一することを確認する。"""
    raw = '{"items": [{"article_id": 1, "summary_ja": "", "why_important": ""}, {"article_id": 2, "summary_ja": "推測の要約", "why_important": "理由", "is_insufficient_info": true}]}'
    parsed = parse_and_validate_llm_json(raw, ArticleSummaryBatchResponseSchema)

    assert parsed.items[0].summary_ja == INSUFFICIENT_INFO_TEXT
    assert parsed.items[0].why_important == INSUFFICIENT_INFO_TEXT
    assert parsed.items[0].is_insufficient_info is True
    assert parsed.items[1].summary_ja == INSUFFICIENT_INFO_TEXT
    assert parsed.items[1].is_insufficient_info is True


# -----------------------------------------------------------------------------
# 2. Gemini / OpenRouter 無料ポリシー強制・有料モデル遮断・リトライ上限
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("paid_model_id", sorted(PAID_COMPARISON_MODEL_IDS))
def test_providers_block_all_paid_comparison_models(paid_model_id: str) -> None:
    """比較対象有料3モデルはいかなるProviderからも呼び出せず policy_violation で遮断されることを確認する。"""
    fake_gem = FakeGenaiClient()
    gem = GeminiProvider(
        api_key="dummy-key",
        enabled=True,
        free_tier_confirmed=True,
        genai_client=fake_gem,
    )
    orc = OpenRouterProvider(api_key="dummy-key", enabled=True)

    gem_res = gem.generate_structured(
        model_id=paid_model_id,
        system_prompt="sys",
        user_prompt="usr",
        response_schema=ArticleClassificationBatchResponseSchema,
    )
    orc_res = orc.generate_structured(
        model_id=paid_model_id,
        system_prompt="sys",
        user_prompt="usr",
        response_schema=ArticleClassificationBatchResponseSchema,
    )

    assert gem_res.success is False
    assert gem_res.error_type == LLM_ERROR_POLICY_VIOLATION
    assert fake_gem.call_count == 0
    assert orc_res.success is False
    assert orc_res.error_type == LLM_ERROR_POLICY_VIOLATION


def test_gemini_blocks_when_free_tier_unconfirmed_or_not_in_allowlist() -> None:
    """GeminiでFree Tier未確認または許可リスト外モデルは実行前に遮断されることを確認する。"""
    fake_client = FakeGenaiClient()
    unconfirmed_gem = GeminiProvider(
        api_key="dummy-key",
        enabled=True,
        free_tier_confirmed=False,
        genai_client=fake_client,
    )
    res1 = unconfirmed_gem.generate_structured(
        model_id="gemini-2.0-flash",
        system_prompt="sys",
        user_prompt="usr",
        response_schema=ArticleClassificationBatchResponseSchema,
    )
    assert res1.success is False
    assert res1.error_type == LLM_ERROR_POLICY_VIOLATION

    confirmed_gem = GeminiProvider(
        api_key="dummy-key",
        enabled=True,
        free_tier_confirmed=True,
        genai_client=fake_client,
    )
    res2 = confirmed_gem.generate_structured(
        model_id="gemini-2.5-pro-experimental-paid",
        system_prompt="sys",
        user_prompt="usr",
        response_schema=ArticleClassificationBatchResponseSchema,
    )
    assert res2.success is False
    assert res2.error_type == LLM_ERROR_POLICY_VIOLATION
    assert fake_client.call_count == 0


def test_openrouter_blocks_non_free_or_positive_price_models() -> None:
    """OpenRouterで :free でないモデルや正の単価を持つモデルが遮断されることを確認する。"""
    orc = OpenRouterProvider(api_key="dummy-key", enabled=True)

    res_non_free = orc.generate_structured(
        model_id="anthropic/claude-3.5-sonnet",
        system_prompt="sys",
        user_prompt="usr",
        response_schema=ArticleClassificationBatchResponseSchema,
    )
    assert res_non_free.success is False
    assert res_non_free.error_type == LLM_ERROR_POLICY_VIOLATION

    res_priced = orc.generate_structured(
        model_id="some-vendor/sneaky-model:free",
        system_prompt="sys",
        user_prompt="usr",
        response_schema=ArticleClassificationBatchResponseSchema,
        catalog_meta={"input_price_per_1m": 0.1, "output_price_per_1m": 0.0},
    )
    assert res_priced.success is False
    assert res_priced.error_type == LLM_ERROR_POLICY_VIOLATION


def test_provider_validation_retry_is_bounded() -> None:
    """LLMが不正JSONを返し続けても max_retries 回で確実に停止し無限リトライしないことを確認する。"""
    fake_client = FakeGenaiClient(response_texts=["not json at all"] * 10)
    gem = GeminiProvider(
        api_key="dummy-key",
        enabled=True,
        free_tier_confirmed=True,
        genai_client=fake_client,
    )

    result = gem.generate_structured(
        model_id="gemini-2.0-flash",
        system_prompt="sys",
        user_prompt="usr",
        response_schema=ArticleClassificationBatchResponseSchema,
        max_retries=2,
    )

    assert result.success is False
    assert result.error_type == LLM_ERROR_VALIDATION
    assert result.attempt_count == 3
    assert fake_client.call_count == 3


# -----------------------------------------------------------------------------
# 3. モデル自動発見・許可リスト照合・自動切替ルーター
# -----------------------------------------------------------------------------


def test_refresh_model_catalog_filters_free_vs_unverified_and_syncs_prices(
    db_session,
) -> None:
    """models.list / OpenRouter一覧から許可リスト一致のみ verified_free とし、有料単価も同期することを確認する。"""
    fake_gem = FakeGenaiClient(
        models_list=[
            SimpleNamespace(
                name="models/gemini-2.0-flash",
                display_name="Gemini 2.0 Flash",
                input_token_limit=1000000,
                supported_actions=["generateContent"],
            ),
            SimpleNamespace(
                name="models/gemini-ultra-unknown",
                display_name="Gemini Ultra Unknown",
                input_token_limit=100000,
                supported_actions=["generateContent"],
            ),
            SimpleNamespace(
                name="models/gemini-3.8-flash",
                display_name="Gemini 3.8 Flash",
                input_token_limit=1000000,
                supported_actions=["generateContent"],
            ),
        ]
    )
    gem_provider = GeminiProvider(
        api_key="dummy",
        enabled=True,
        free_tier_confirmed=True,
        genai_client=fake_gem,
    )

    def or_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "meta-llama/llama-3.3-70b-instruct:free",
                        "name": "Llama 3.3 70B Free",
                        "pricing": {"prompt": "0", "completion": "0", "request": "0"},
                    },
                    {
                        "id": "deepseek/deepseek-v4.1-flash",
                        "name": "DeepSeek V4.1 Flash",
                        "pricing": {"prompt": "0.00000014", "completion": "0.00000028"},
                    },
                ]
            },
            request=request,
        )

    or_client = _make_openrouter_mock_client(or_handler)
    try:
        or_provider = OpenRouterProvider(
            api_key="dummy",
            enabled=True,
            http_client=or_client,
        )
        res = refresh_model_catalog(
            session=db_session,
            gemini_provider=gem_provider,
            openrouter_provider=or_provider,
        )
    finally:
        or_client.close()

    assert res["executable_free_count"] >= 3
    unknown_model = db_session.get(ModelCatalog, "gemini-ultra-unknown")
    assert unknown_model is not None
    assert unknown_model.free_tier_status == FREE_STATUS_UNVERIFIED
    assert unknown_model.is_executable_free_model is False

    paid_gem = db_session.get(ModelCatalog, "gemini-3.8-flash")
    assert paid_gem is not None
    assert paid_gem.free_tier_status == FREE_STATUS_PAID_OR_CONDITIONAL
    assert paid_gem.is_executable_free_model is False

    llama_free = db_session.get(ModelCatalog, "meta-llama/llama-3.3-70b-instruct:free")
    assert llama_free is not None
    assert llama_free.free_tier_status == FREE_STATUS_VERIFIED
    assert llama_free.is_executable_free_model is True


def test_router_switches_from_rate_limited_gemini_to_openrouter_free(
    db_session,
) -> None:
    """第1候補の無料モデルが429レート制限のとき、別プロバイダーの無料モデルへ自動切替することを確認する。"""
    seed_default_models_and_prices(db_session)
    setting_repo = SettingRepository(db_session)
    setting_repo.update_settings(
        {
            "llm_enabled": True,
            "selection_mode": "gemini",
            "preferred_model_id": "gemini-2.0-flash",
            "auto_switch_enabled": True,
        }
    )
    run_obj = RunRepository(db_session).create_run(run_type="manual")

    # Gemini は全モデル 429 エラーを返す
    failing_gem = GeminiProvider(
        api_key="dummy",
        enabled=True,
        free_tier_confirmed=True,
        genai_client=FakeGenaiClient(
            raise_exc=ProviderHttpError("429 Resource has been exhausted (rate limit)", 429)
        ),
    )

    # OpenRouter (openrouter/free) は正常な分類JSONを返す
    def or_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"items": [{"article_id": 10, "category": "ai_llm", "importance_score": 90}]}'
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 80,
                    "completion_tokens": 30,
                    "total_tokens": 110,
                },
            },
            request=request,
        )

    or_client = _make_openrouter_mock_client(or_handler)
    try:
        working_orc = OpenRouterProvider(
            api_key="dummy",
            enabled=True,
            http_client=or_client,
        )
        route_res = execute_with_free_model_routing(
            session=db_session,
            run_id=run_obj.run_id,
            purpose="classify",
            system_prompt="sys",
            user_prompt="usr",
            response_schema=ArticleClassificationBatchResponseSchema,
            target_article_count=1,
            gemini_provider=failing_gem,
            openrouter_provider=working_orc,
        )
    finally:
        or_client.close()

    assert route_res["success"] is True
    assert route_res["provider"] == "openrouter"
    assert route_res["model_id"] == "openrouter/free"
    assert route_res["switched_from_model"] is not None
    assert route_res["switch_reason"] == LLM_ERROR_RATE_LIMIT

    gem_catalog = db_session.get(ModelCatalog, "gemini-2.0-flash")
    assert gem_catalog is not None
    assert gem_catalog.availability_status == AVAILABILITY_RATE_LIMITED


def test_router_stops_safely_when_all_free_models_fail_or_auto_switch_off(
    db_session,
) -> None:
    """全無料モデルが失敗した場合や自動切替OFF時は有料モデルを呼ばず ai_pending で安全停止することを確認する。"""
    seed_default_models_and_prices(db_session)
    SettingRepository(db_session).update_settings(
        {
            "llm_enabled": True,
            "selection_mode": "gemini",
            "preferred_model_id": "gemini-2.0-flash",
            "auto_switch_enabled": False,
        }
    )
    run_obj = RunRepository(db_session).create_run(run_type="manual")

    failing_gem = GeminiProvider(
        api_key="dummy",
        enabled=True,
        free_tier_confirmed=True,
        genai_client=FakeGenaiClient(
            raise_exc=ProviderHttpError("Quota exceeded", 429)
        ),
    )
    orc = OpenRouterProvider(api_key="dummy", enabled=True)

    candidates = build_candidate_models_order(
        session=db_session,
        selection_mode="gemini",
        preferred_model_id="gemini-2.0-flash",
        auto_switch_enabled=False,
    )
    assert len(candidates) == 1

    res = execute_with_free_model_routing(
        session=db_session,
        run_id=run_obj.run_id,
        purpose="classify",
        system_prompt="sys",
        user_prompt="usr",
        response_schema=ArticleClassificationBatchResponseSchema,
        gemini_provider=failing_gem,
        openrouter_provider=orc,
    )

    assert res["success"] is False
    assert res["ai_pending"] is True
    assert res["reason"] == "all_free_models_failed"


def test_router_stops_when_daily_call_limit_reached(db_session) -> None:
    """1日のAPI呼び出し上限に達した場合はLLMを呼び出さず ai_pending で停止することを確認する。"""
    seed_default_models_and_prices(db_session)
    SettingRepository(db_session).update_settings(
        {
            "llm_enabled": True,
            "daily_api_call_limit": 1,
        }
    )
    run_obj = RunRepository(db_session).create_run(run_type="manual")

    fake_gem = FakeGenaiClient(
        response_texts=['{"items": [{"article_id": 1, "category": "ai_llm", "importance_score": 80}]}']
    )
    gem = GeminiProvider(
        api_key="dummy",
        enabled=True,
        free_tier_confirmed=True,
        genai_client=fake_gem,
    )

    first = execute_with_free_model_routing(
        session=db_session,
        run_id=run_obj.run_id,
        purpose="classify",
        system_prompt="sys",
        user_prompt="usr",
        response_schema=ArticleClassificationBatchResponseSchema,
        gemini_provider=gem,
    )
    assert first["success"] is True

    second = execute_with_free_model_routing(
        session=db_session,
        run_id=run_obj.run_id,
        purpose="summarize",
        system_prompt="sys",
        user_prompt="usr",
        response_schema=ArticleSummaryBatchResponseSchema,
        gemini_provider=gem,
    )
    assert second["success"] is False
    assert second["ai_pending"] is True
    assert second["reason"] == "daily_call_limit_reached"
    assert fake_gem.call_count == 1


# -----------------------------------------------------------------------------
# 4. AI分類・要約パイプライン、カテゴリ再選別（再利用）、ai_pending 保留/再開 (Step 5-1)
# -----------------------------------------------------------------------------


def _patch_single_article_rss(monkeypatch: pytest.MonkeyPatch) -> None:
    """テスト用の記事2件を返すよう `collect_rss` を差し替える。"""
    items = [
        {
            "source_id": 1,
            "source_name": "テストAIメディア",
            "guid": "ai-101",
            "url": "https://example.com/ai-101",
            "normalized_url": "https://example.com/ai-101",
            "title": "新型AIエージェントが開発効率を大幅改善",
            "published_at": BASE_PUB_UTC,
            "updated_at_rss": None,
            "is_pubdate_verified": True,
            "rss_categories": ["AI"],
            "summary_snippet": "自律型AIエージェントによるコード生成と検証の自動化が進んでいる。",
            "primary_category": "ai_llm",
            "importance_score": 70,
            "is_excluded": False,
            "exclusion_reason": None,
        },
        {
            "source_id": 1,
            "source_name": "テスト経済メディア",
            "guid": "fin-102",
            "url": "https://example.com/fin-102",
            "normalized_url": "https://example.com/fin-102",
            "title": "日銀が金融政策決定会合で政策金利を据え置き",
            "published_at": BASE_PUB_UTC,
            "updated_at_rss": None,
            "is_pubdate_verified": True,
            "rss_categories": ["経済"],
            "summary_snippet": "物価と賃金の動向を見極めるため現行の金融緩和水準を維持した。",
            "primary_category": "economy_finance",
            "importance_score": 65,
            "is_excluded": False,
            "exclusion_reason": None,
        },
    ]

    def fake_collect(_payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "all_fetched_items": items,
            "target_items": items,
            "unverified_pubdate_count": 0,
            "out_of_range_notice": None,
            "source_results": [
                {
                    "source_id": 1,
                    "source_name": "テストAIメディア",
                    "success": True,
                    "fetched_count": 2,
                    "error": None,
                }
            ],
            "success_sources": 1,
            "failed_sources": 0,
            "rss_failure_rate": 0.0,
        }

    monkeypatch.setattr(digest_orchestrator, "collect_rss", fake_collect)


def test_digest_run_with_llm_and_recategorize_reuses_analysis_with_zero_new_tokens(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AI分類・要約後にカテゴリを変更すると、RSS再取得せず既存分析を再利用し新規トークン0になることを確認する。"""
    _patch_single_article_rss(monkeypatch)
    seed_default_models_and_prices(db_session)
    SettingRepository(db_session).update_settings(
        {
            "is_initial_approved": True,
            "llm_enabled": True,
            "selected_categories": ["ai_llm", "economy_finance"],
        }
    )

    fake_gem = FakeGenaiClient(
        response_texts=[
            '{"items": [{"article_id": 1, "category": "ai_llm", "importance_score": 92}, {"article_id": 2, "category": "economy_finance", "importance_score": 88}]}',
            '{"items": [{"article_id": 1, "summary_ja": "AIエージェントの要約です。", "why_important": "開発生産性に直結するため。"}, {"article_id": 2, "summary_ja": "日銀金融政策の要約です。", "why_important": "金利動向に影響するため。"}]}',
        ]
    )
    gem = GeminiProvider(
        api_key="dummy",
        enabled=True,
        free_tier_confirmed=True,
        genai_client=fake_gem,
    )

    run_res = digest_orchestrator.execute_digest_run(
        session=db_session,
        run_type="manual",
        gemini_provider=gem,
    )
    assert run_res["status"] == RUN_STATUS_COMPLETED
    assert fake_gem.call_count == 2

    # カテゴリを economy_finance のみに変更して再選別
    recat_res = digest_orchestrator.recategorize_from_saved_articles(
        session=db_session,
        selected_categories=["economy_finance"],
        gemini_provider=gem,
    )
    assert recat_res["reused_analysis"] is True
    assert recat_res["new_tokens_consumed"] == 0
    assert recat_res["parent_run_id"] == run_res["run_id"]
    # LLM が一切追加呼び出しされていないこと
    assert fake_gem.call_count == 2

    view_data = digest_orchestrator.build_view_data_for_run(
        session=db_session,
        run_id=recat_res["run_id"],
    )
    assert len(view_data["cards"]) == 1
    assert view_data["cards"][0]["summary_ja"] == "日銀金融政策の要約です。"
    assert view_data["summary"]["total_tokens"] == 0
    assert "再利用" in view_data["summary"]["usage_measurement_status"]


def test_ai_pending_fallback_and_resume_pending_run(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全無料モデル障害時にRSS記事を保存して ai_pending とし、回復後に再開して要約を完了できることを確認する。"""
    _patch_single_article_rss(monkeypatch)
    seed_default_models_and_prices(db_session)
    SettingRepository(db_session).update_settings(
        {
            "is_initial_approved": True,
            "llm_enabled": True,
            "selected_categories": ["ai_llm", "economy_finance"],
        }
    )

    # 最初は Gemini も OpenRouter も失敗し、全無料モデルが利用不能な状態を模擬
    failing_gem = GeminiProvider(
        api_key="dummy",
        enabled=True,
        free_tier_confirmed=True,
        genai_client=FakeGenaiClient(
            raise_exc=ProviderHttpError("Service Unavailable", 503)
        ),
    )
    unconfigured_orc = OpenRouterProvider(api_key="", enabled=False)

    pending_res = digest_orchestrator.execute_digest_run(
        session=db_session,
        run_type="manual",
        gemini_provider=failing_gem,
        openrouter_provider=unconfigured_orc,
    )
    assert pending_res["status"] == RUN_STATUS_AI_PENDING
    assert pending_res["displayed_count"] == 2

    # 保留中でもRSS記事は閲覧可能であること
    pending_view = digest_orchestrator.build_view_data_for_run(
        session=db_session,
        run_id=pending_res["run_id"],
    )
    assert len(pending_view["cards"]) == 2
    assert "保留中" in pending_view["cards"][0]["summary_ja"]

    # モデル回復後に resume_pending_ai_run で再開
    recovered_gem = GeminiProvider(
        api_key="dummy",
        enabled=True,
        free_tier_confirmed=True,
        genai_client=FakeGenaiClient(
            response_texts=[
                '{"items": [{"article_id": 1, "category": "ai_llm", "importance_score": 95}, {"article_id": 2, "category": "economy_finance", "importance_score": 85}]}',
                '{"items": [{"article_id": 1, "summary_ja": "再開後のAI要約1", "why_important": "理由1"}, {"article_id": 2, "summary_ja": "再開後のAI要約2", "why_important": "理由2"}]}',
            ]
        ),
    )
    # モデルの availability を available に戻す
    for m in db_session.query(ModelCatalog).all():
        m.availability_status = "available"
    db_session.commit()

    resumed = digest_orchestrator.resume_pending_ai_run(
        session=db_session,
        run_id=pending_res["run_id"],
        gemini_provider=recovered_gem,
        openrouter_provider=unconfigured_orc,
    )
    assert resumed["resumed"] is True
    assert resumed["status"] == RUN_STATUS_COMPLETED
    assert resumed["parent_run_id"] == pending_res["run_id"]
    assert resumed["summarized_count"] == 2

    completed_view = digest_orchestrator.build_view_data_for_run(
        session=db_session,
        run_id=resumed["run_id"],
    )
    assert completed_view["cards"][0]["summary_ja"] == "再開後のAI要約1"
