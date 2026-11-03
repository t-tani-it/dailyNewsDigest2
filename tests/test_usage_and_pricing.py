"""Phase 3 (Step 4-4〜4-5): 1実行単位（`run_id`）トークン使用量集計・有料3モデル参考料金・サマリー表示のテスト。

目的（Why）:
    1. ニュースカード単位ではなく 1実行単位（`run_id`）でトークンが集計されること
    2. 推論トークンが出力に内包されている場合に二重加算されないこと
    3. トークン未計測（`None`）を `0` と誤表示しないこと
    4. 比較対象有料3モデルの価格未確認・トークン未計測時に「算出不可」と表示すること
    5. 小額の参考料金を `$0.00` と誤表示しないこと
    を自動検証する（指示書 §14, §15, §16, §17.3〜§17.5）。
仕様（What）:
    - `aggregate_run_token_usage`: 全リクエスト合算・推論内訳・未計測/一部未計測判定
    - `calculate_paid_model_estimates`: 3モデル参考料金算出・算出不可判定・小額フォーマット
    - `index.html`: サマリーが1ページ1箇所のみ表示され、カード内に料金やトークンが出ないこと
前提条件・依存関係（Assumption / Dependencies）:
    pytest, `app.services.usage_service`, `app.services.pricing_service`, `app.schemas.run_schema`
入出力（I/O）:
    テスト関数ごとに assert で合否を判定する。
注意点（Caution）:
    比較対象有料3モデルは計算専用であり、いかなるテストでもAPI呼び出しは行わない。
今後の拡張ポイント（Future Work）:
    月次累計トークン・参考料金集計テストの追加。
変更履歴（Change Log）:
    2026-11-03 - 初版作成（Step 4-5）
"""

from __future__ import annotations

from app.models.usage_log import LlmUsageLog
from app.repositories.run_repo import RunRepository
from app.repositories.setting_repo import SettingRepository
from app.schemas.llm_schema import LlmProviderCallResultSchema, LlmTokenUsageSchema
from app.schemas.run_schema import REFERENCE_PRICE_DISCLAIMER, format_usd_cost
from app.services.model_router_service import record_llm_call_log
from app.services.pricing_service import (
    calculate_paid_model_estimates,
    compute_single_model_reference_cost,
    upsert_price_snapshot,
)
from app.services.usage_service import (
    MEASUREMENT_STATE_MEASURED,
    MEASUREMENT_STATE_PARTIAL,
    MEASUREMENT_STATE_REUSED,
    MEASUREMENT_STATE_UNMEASURED,
    aggregate_run_token_usage,
    get_usage_dashboard_data,
)

# -----------------------------------------------------------------------------
# 1. トークン使用量集計・推論トークン二重計上防止・未計測分離
# -----------------------------------------------------------------------------


def test_aggregate_run_token_usage_sums_requests_and_prevents_double_counting(
    db_session,
) -> None:
    """1実行内の分類・要約リクエストを合算し、出力内包の推論トークンを二重加算しないことを確認する。"""
    run_obj = RunRepository(db_session).create_run(run_type="manual")

    # 1回目（classify）: input=1000, output=300（うち推論100内包） -> 合計1300
    call_1 = LlmProviderCallResultSchema(
        success=True,
        provider="openrouter",
        model_id="openrouter/free",
        purpose="classify",
        usage=LlmTokenUsageSchema(
            input_tokens=1000,
            output_tokens=300,
            reasoning_tokens=100,
            total_tokens=1400,  # 万一APIが二重加算した1400を返しても1300へ補正される
            reasoning_included_in_output=True,
        ),
    )
    record_llm_call_log(db_session, run_obj.run_id, call_1, target_article_count=10)

    # 2回目（summarize）: input=2000, output=500（推論50内包） -> 合計2500
    call_2 = LlmProviderCallResultSchema(
        success=True,
        provider="openrouter",
        model_id="openrouter/free",
        purpose="summarize",
        usage=LlmTokenUsageSchema(
            input_tokens=2000,
            output_tokens=500,
            reasoning_tokens=50,
            total_tokens=2500,
            reasoning_included_in_output=True,
        ),
    )
    record_llm_call_log(db_session, run_obj.run_id, call_2, target_article_count=5)

    agg = aggregate_run_token_usage(db_session, run_obj.run_id)

    assert agg["api_request_count"] == 2
    assert agg["measured_request_count"] == 2
    assert agg["total_input_tokens"] == 3000
    assert agg["total_output_tokens"] == 800
    assert agg["total_reasoning_tokens"] == 150
    assert agg["total_tokens"] == 3800
    assert agg["measurement_state"] == MEASUREMENT_STATE_MEASURED


def test_aggregate_run_token_usage_distinguishes_unmeasured_from_zero(
    db_session,
) -> None:
    """APIからトークン数が返らなかった場合に 0 ではなく None（unmeasured）を返すことを確認する。"""
    run_obj = RunRepository(db_session).create_run(run_type="manual")

    unmeasured_call = LlmProviderCallResultSchema(
        success=True,
        provider="openrouter",
        model_id="openrouter/free",
        purpose="classify",
        usage=LlmTokenUsageSchema(input_tokens=None, output_tokens=None),
    )
    record_llm_call_log(db_session, run_obj.run_id, unmeasured_call)

    agg = aggregate_run_token_usage(db_session, run_obj.run_id)

    assert agg["api_request_count"] == 1
    assert agg["total_input_tokens"] is None
    assert agg["total_output_tokens"] is None
    assert agg["total_tokens"] is None
    assert agg["measurement_state"] == MEASUREMENT_STATE_UNMEASURED
    assert "未計測" in agg["usage_measurement_status"]


def test_aggregate_run_token_usage_handles_partial_unmeasured(db_session) -> None:
    """複数リクエストのうち一部のみ計測できた場合は partial_unmeasured になることを確認する。"""
    run_obj = RunRepository(db_session).create_run(run_type="manual")

    measured_call = LlmProviderCallResultSchema(
        success=True,
        provider="gemini",
        model_id="gemini-2.0-flash",
        purpose="classify",
        usage=LlmTokenUsageSchema(input_tokens=500, output_tokens=100),
    )
    unmeasured_call = LlmProviderCallResultSchema(
        success=True,
        provider="openrouter",
        model_id="openrouter/free",
        purpose="summarize",
        usage=LlmTokenUsageSchema(input_tokens=None, output_tokens=None),
    )
    record_llm_call_log(db_session, run_obj.run_id, measured_call)
    record_llm_call_log(db_session, run_obj.run_id, unmeasured_call)

    agg = aggregate_run_token_usage(db_session, run_obj.run_id)

    assert agg["measurement_state"] == MEASUREMENT_STATE_PARTIAL
    assert agg["total_input_tokens"] == 500
    assert agg["total_output_tokens"] == 100
    assert agg["total_tokens"] == 600
    assert "一部未計測" in agg["usage_measurement_status"]


def test_aggregate_run_token_usage_reused_analysis_reports_zero_new_tokens(
    db_session,
) -> None:
    """既存分析再利用時は過去のトークンを再計上せず新規トークン0になることを確認する。"""
    run_obj = RunRepository(db_session).create_run(
        run_type="recategorize",
        reused_analysis=True,
    )

    agg = aggregate_run_token_usage(db_session, run_obj.run_id, reused_analysis=True)

    assert agg["total_tokens"] == 0
    assert agg["measurement_state"] == MEASUREMENT_STATE_REUSED
    assert "既存分析再利用" in agg["usage_measurement_status"]


# -----------------------------------------------------------------------------
# 2. 有料3モデルの参考料金計算・算出不可判定・小額表示
# -----------------------------------------------------------------------------


def test_calculate_paid_model_estimates_returns_uncalculable_when_price_unverified(
    db_session,
) -> None:
    """初期状態（価格未確認）では比較対象3モデルすべてが『算出不可』になることを確認する。"""
    estimates = calculate_paid_model_estimates(
        session=db_session,
        total_input_tokens=10000,
        total_output_tokens=2000,
        measurement_state=MEASUREMENT_STATE_MEASURED,
    )

    assert len(estimates) == 3
    for est in estimates:
        assert est.is_calculable is False
        assert est.display_cost == "算出不可"


def test_calculate_paid_model_estimates_computes_exact_usd_when_verified(
    db_session,
) -> None:
    """価格スナップショット確認済みのモデルは正確にUSD参考料金を算出し、未確認モデルは算出不可のままであることを確認する。"""
    upsert_price_snapshot(
        session=db_session,
        model_id="deepseek/deepseek-v4.1-flash",
        input_price_per_1m=0.14,
        output_price_per_1m=0.28,
        is_verified=True,
        source_url="https://openrouter.ai/api/v1/models",
    )
    upsert_price_snapshot(
        session=db_session,
        model_id="openai/gpt-6-luna",
        input_price_per_1m=0.50,
        output_price_per_1m=1.50,
        is_verified=True,
        source_url="https://openrouter.ai/api/v1/models",
    )

    # input=10,000 tokens, output=2,000 tokens
    # DeepSeek: (10000/1M)*0.14 + (2000/1M)*0.28 = 0.0014 + 0.00056 = $0.001960
    # GPT-6 Luna: (10000/1M)*0.50 + (2000/1M)*1.50 = 0.0050 + 0.0030 = $0.008000
    estimates = calculate_paid_model_estimates(
        session=db_session,
        total_input_tokens=10000,
        total_output_tokens=2000,
        measurement_state=MEASUREMENT_STATE_MEASURED,
        total_reasoning_tokens=500,
        reasoning_included_in_output=True,
    )
    by_id = {e.model_id: e for e in estimates}

    ds = by_id["deepseek/deepseek-v4.1-flash"]
    assert ds.is_calculable is True
    assert ds.display_cost == "$0.001960"
    assert ds. display_cost != "$0.00"

    luna = by_id["openai/gpt-6-luna"]
    assert luna.is_calculable is True
    assert luna.display_cost == "$0.008000"

    # gemini-3.8-flash は価格未登録のため算出不可
    gem = by_id["gemini-3.8-flash"]
    assert gem.is_calculable is False
    assert gem.display_cost == "算出不可"


def test_calculate_paid_model_estimates_returns_uncalculable_when_tokens_unmeasured(
    db_session,
) -> None:
    """価格が確認済みでもトークン使用量が未計測（None）なら『算出不可』になることを確認する。"""
    upsert_price_snapshot(
        session=db_session,
        model_id="deepseek/deepseek-v4.1-flash",
        input_price_per_1m=0.14,
        output_price_per_1m=0.28,
        is_verified=True,
    )

    estimates = calculate_paid_model_estimates(
        session=db_session,
        total_input_tokens=None,
        total_output_tokens=None,
        measurement_state=MEASUREMENT_STATE_UNMEASURED,
    )
    ds = next(e for e in estimates if e.model_id == "deepseek/deepseek-v4.1-flash")
    assert ds.is_calculable is False
    assert ds.display_cost == "算出不可"


def test_format_usd_cost_never_displays_zero_point_zero_zero_for_tiny_amounts() -> None:
    """極小の非ゼロ金額を $0.00 と誤表示せず、0のときだけ $0 と表示することを確認する。"""
    assert format_usd_cost(0.0, is_verified=True) == "$0"
    assert format_usd_cost(0.000015, is_verified=True) == "$0.000015"
    assert format_usd_cost(0.00000025, is_verified=True) == "$0.00000025"
    assert format_usd_cost(0.00000025, is_verified=True) != "$0.00"
    assert format_usd_cost(0.0004, is_verified=False) == "算出不可"


def test_compute_single_model_reference_cost_handles_separate_reasoning_tokens() -> None:
    """推論トークンが出力トークンと別計上（reasoning_included_in_output=False）の場合のみ加算されることを確認する。"""
    cost_included = compute_single_model_reference_cost(
        input_tokens=1000,
        output_tokens=500,
        input_price_per_1m=1.0,
        output_price_per_1m=2.0,
        reasoning_tokens=500,
        reasoning_included_in_output=True,
    )
    cost_separate = compute_single_model_reference_cost(
        input_tokens=1000,
        output_tokens=500,
        input_price_per_1m=1.0,
        output_price_per_1m=2.0,
        reasoning_tokens=500,
        reasoning_included_in_output=False,
    )

    assert cost_included == 0.002
    assert cost_separate == 0.003


# -----------------------------------------------------------------------------
# 3. 画面サマリー1箇所表示・カード内非表示・ダッシュボード集計
# -----------------------------------------------------------------------------


def test_index_page_renders_single_usage_summary_and_disclaimer(
    client, db_session
) -> None:
    """基本画面にAI処理・使用量サマリーが1つだけ表示され、必須注意書きが含まれることを確認する。"""
    SettingRepository(db_session).approve_initial_onboarding(["ai_llm"])

    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    assert html.count('id="ai-usage-summary"') == 1
    assert REFERENCE_PRICE_DISCLAIMER in html
    assert "LLMモデル選択（無料利用確認済みモデル限定）" in html


def test_usage_dashboard_data_aggregates_models_and_switches(db_session) -> None:
    """ダッシュボード用集計がモデル別呼び出し回数と自動切替履歴を返すことを確認する。"""
    run_obj = RunRepository(db_session).create_run(run_type="manual")
    log_row = LlmUsageLog(
        run_id=run_obj.run_id,
        purpose="classify",
        provider="openrouter",
        model_id="openrouter/free",
        input_tokens=400,
        output_tokens=100,
        total_tokens=500,
        usage_status="measured",
        switched_from_model="gemini-2.0-flash",
        switch_reason="rate_limit",
    )
    db_session.add(log_row)
    db_session.commit()

    dashboard = get_usage_dashboard_data(db_session)

    assert dashboard["daily_limit_status"]["today_calls"] == 1
    assert len(dashboard["model_breakdown"]) == 1
    assert dashboard["model_breakdown"][0]["total_tokens"] == 500
    assert len(dashboard["switch_history"]) == 1
    assert dashboard["switch_history"][0]["from_model"] == "gemini-2.0-flash"
