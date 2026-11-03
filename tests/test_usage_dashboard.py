"""Phase 4（Step 5-2）: 過去実行履歴画面・AI使用量ダッシュボードのテスト。

目的（Why）:
    run_id 単位のトークン集計、モデル別呼び出し回数、自動切替履歴、
    比較対象有料3モデルの単価確認状態を画面とJSON APIで正しく表示し、
    未計測（None）を 0 と誤表示しないことを自動検証する（指示書 §14〜§18）。
仕様（What）:
    - `build_usage_dashboard` / `build_run_usage_map` の集計
    - `GET /usage` のHTML表示
    - `GET /api/usage` のJSON返却と存在しない run_id の404
    - 過去実行履歴画面の「未計測」表示
前提条件・依存関係（Assumption / Dependencies）:
    pytest, FastAPI `TestClient`, `app.services.usage_dashboard_service`
入出力（I/O）:
    テスト関数ごとに assert で合否を判定する。
注意点（Caution）:
    比較対象有料3モデルは参考料金計算専用であり、外部API呼び出しは行わない。
今後の拡張ポイント（Future Work）:
    月次集計・期間指定ダッシュボードのテスト。
変更履歴（Change Log）:
    2026-11-03 - 初版作成（Step 5-2）
"""

from __future__ import annotations

from app.models.usage_log import LlmUsageLog
from app.repositories.run_repo import RunRepository
from app.repositories.setting_repo import SettingRepository
from app.services.pricing_service import upsert_price_snapshot
from app.services.usage_dashboard_service import (
    build_run_usage_map,
    build_usage_dashboard,
)
from app.services.usage_service import (
    MEASUREMENT_STATE_PARTIAL,
    MEASUREMENT_STATE_REUSED,
    MEASUREMENT_STATE_UNMEASURED,
    summarize_usage_logs,
)

# =============================================================================
# 設定値ブロック（テスト用固定値）
# =============================================================================

VERIFIED_PRICE_SOURCE = "https://openrouter.ai/api/v1/models"


def _add_log(
    db_session,
    run_id: str,
    *,
    input_tokens: int | None,
    output_tokens: int | None,
    reasoning_tokens: int | None = None,
    total_tokens: int | None = None,
    usage_status: str = "measured",
    switched_from_model: str | None = None,
    switch_reason: str | None = None,
) -> LlmUsageLog:
    """テスト用のLLM利用履歴を1件追加する。"""
    log = LlmUsageLog(
        run_id=run_id,
        purpose="classify",
        provider="openrouter",
        model_id="openrouter/free",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        total_tokens=total_tokens,
        usage_status=usage_status,
        switched_from_model=switched_from_model,
        switch_reason=switch_reason,
    )
    db_session.add(log)
    db_session.commit()
    return log


def test_usage_dashboard_aggregates_overview_models_switches_and_prices(db_session) -> None:
    """ダッシュボードが直近ログの合計・モデル別・切替履歴・有料3モデル単価を返すことを確認する。"""
    run_obj = RunRepository(db_session).create_run(run_type="manual")
    _add_log(
        db_session,
        run_obj.run_id,
        input_tokens=1000,
        output_tokens=300,
        reasoning_tokens=100,
        total_tokens=1300,
        switched_from_model="gemini-2.0-flash",
        switch_reason="rate_limit",
    )
    _add_log(
        db_session,
        run_obj.run_id,
        input_tokens=None,
        output_tokens=None,
        usage_status="unmeasured",
    )

    dashboard = build_usage_dashboard(
        session=db_session,
        limit_logs=100,
        limit_runs=10,
    )

    overview = dashboard["overview"]
    assert overview["total_calls"] == 2
    assert overview["measured_count"] == 1
    assert overview["unmeasured_count"] == 1
    assert overview["total_input_tokens"] == 1000
    assert overview["total_output_tokens"] == 300
    assert overview["total_tokens"] == 1300
    assert overview["switch_count"] == 1

    assert len(dashboard["model_breakdown"]) == 1
    model_row = dashboard["model_breakdown"][0]
    assert model_row["call_count"] == 2
    assert model_row["has_unmeasured"] is True
    assert model_row["total_tokens"] == 1300

    assert len(dashboard["switch_history"]) == 1
    assert dashboard["switch_history"][0]["from_model"] == "gemini-2.0-flash"

    assert len(dashboard["recent_run_usages"]) == 1
    assert dashboard["recent_run_usages"][0]["run_id"] == run_obj.run_id

    assert len(dashboard["price_snapshots"]) == 3
    assert all(p["status_label"] == "未確認（算出不可）" for p in dashboard["price_snapshots"])
    assert "参考値" in dashboard["disclaimer"] or "参考" in dashboard["disclaimer"]


def test_usage_dashboard_marks_unmeasured_and_partial_states(db_session) -> None:
    """未計測のみと一部未計測を区別し、0 tokens と誤表示しないことを確認する。"""
    run_obj = RunRepository(db_session).create_run(run_type="manual")

    _add_log(
        db_session,
        run_obj.run_id,
        input_tokens=None,
        output_tokens=None,
        usage_status="unmeasured",
    )

    unmeasured = build_usage_dashboard(db_session)
    assert unmeasured["overview"]["measurement_state"] == MEASUREMENT_STATE_UNMEASURED
    assert unmeasured["overview"]["total_tokens"] is None
    assert "未計測" in unmeasured["overview"]["measurement_label"]

    measured_run = RunRepository(db_session).create_run(run_type="manual")
    _add_log(db_session, measured_run.run_id, input_tokens=500, output_tokens=100)
    _add_log(
        db_session,
        measured_run.run_id,
        input_tokens=None,
        output_tokens=None,
        usage_status="unmeasured",
    )

    partial = build_usage_dashboard(db_session)
    assert partial["overview"]["measurement_state"] == MEASUREMENT_STATE_PARTIAL
    assert partial["overview"]["total_tokens"] == 600
    assert "一部未計測" in partial["overview"]["measurement_label"]


def test_build_run_usage_map_marks_reused_analysis_as_zero_new_tokens(db_session) -> None:
    """既存分析再利用の実行は新規トークン 0・reused 状態になることを確認する。"""
    run_obj = RunRepository(db_session).create_run(
        run_type="recategorize",
        reused_analysis=True,
    )

    usage_map = build_run_usage_map(db_session, [run_obj])

    assert usage_map[run_obj.run_id]["total_tokens"] == 0
    assert usage_map[run_obj.run_id]["measurement_state"] == MEASUREMENT_STATE_REUSED


def test_summarize_usage_logs_ignores_failed_requests_without_tokens(db_session) -> None:
    """失敗かつトークン未取得のリクエストはトークン合計に含めず、失敗回数だけ数えることを確認する。"""
    run_obj = RunRepository(db_session).create_run(run_type="manual")
    failed = _add_log(
        db_session,
        run_obj.run_id,
        input_tokens=None,
        output_tokens=None,
        usage_status="failed",
    )

    result = summarize_usage_logs([failed])

    assert result["total_calls"] == 1
    assert result["failed_count"] == 1
    assert result["measured_count"] == 0
    assert result["total_tokens"] is None


def test_price_snapshot_rows_show_verified_unit_prices(db_session) -> None:
    """価格確認済みの有料モデルは単価と確認状態を表示し、未確認モデルは算出不可のままにすることを確認する。"""
    upsert_price_snapshot(
        session=db_session,
        model_id="deepseek/deepseek-v4.1-flash",
        input_price_per_1m=0.14,
        output_price_per_1m=0.28,
        is_verified=True,
        source_url=VERIFIED_PRICE_SOURCE,
    )

    dashboard = build_usage_dashboard(db_session)
    by_id = {p["model_id"]: p for p in dashboard["price_snapshots"]}

    verified = by_id["deepseek/deepseek-v4.1-flash"]
    assert verified["is_verified"] is True
    assert verified["input_price_display"] == "$0.1400"
    assert verified["output_price_display"] == "$0.2800"
    assert verified["status_label"] == "確認済み"
    assert verified["source_url"] == VERIFIED_PRICE_SOURCE

    unverified = by_id["openai/gpt-6-luna"]
    assert unverified["is_verified"] is False
    assert unverified["status_label"] == "未確認（算出不可）"


def test_usage_page_renders_dashboard_and_policy_notice(client, db_session) -> None:
    """`GET /usage` が使用量ダッシュボード・有料モデル注意書き・ナビを表示することを確認する。"""
    SettingRepository(db_session).approve_initial_onboarding(["ai_llm"])
    run_obj = RunRepository(db_session).create_run(run_type="manual")
    _add_log(
        db_session,
        run_obj.run_id,
        input_tokens=1200,
        output_tokens=300,
        total_tokens=1500,
    )

    response = client.get("/usage")

    assert response.status_code == 200
    html = response.text
    assert "AI使用量・無料モデル運用ダッシュボード" in html
    assert "比較対象有料3モデルの単価確認状態" in html
    assert "算出不可" in html
    assert "API呼び出しは行いません" in html
    assert run_obj.run_id[:8] in html


def test_usage_api_returns_json_and_rejects_unknown_run(client, db_session) -> None:
    """`GET /api/usage` が集計JSONを返し、存在しない run_id では404になることを確認する。"""
    run_obj = RunRepository(db_session).create_run(run_type="manual")
    _add_log(
        db_session,
        run_obj.run_id,
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
    )

    response = client.get("/api/usage", params={"limit_logs": 10, "limit_runs": 5})
    assert response.status_code == 200
    payload = response.json()
    assert payload["overview"]["total_tokens"] == 150
    assert len(payload["price_snapshots"]) == 3
    assert payload["recent_run_usages"][0]["run_id"] == run_obj.run_id

    selected = client.get("/api/usage", params={"run_id": run_obj.run_id})
    assert selected.status_code == 200
    assert selected.json()["selected_run_usage"]["total_tokens"] == 150

    missing = client.get("/api/usage", params={"run_id": "00000000-0000-0000-0000-000000000000"})
    assert missing.status_code == 404


def test_history_page_displays_unmeasured_instead_of_none_tokens(client, db_session) -> None:
    """履歴画面で未計測トークンを `None` と表示せず「未計測」と表示することを確認する。"""
    SettingRepository(db_session).approve_initial_onboarding(["ai_llm"])
    run_obj = RunRepository(db_session).create_run(run_type="manual")
    RunRepository(db_session).finalize_run(
        run_id=run_obj.run_id,
        status="ai_pending",
        fetched_count=2,
        deduped_count=2,
        displayed_count=0,
    )
    _add_log(
        db_session,
        run_obj.run_id,
        input_tokens=None,
        output_tokens=None,
        usage_status="unmeasured",
    )

    response = client.get("/history", params={"run_id": run_obj.run_id})

    assert response.status_code == 200
    html = response.text
    assert "未計測" in html
    assert "None tokens" not in html
    assert "AI使用量ダッシュボード" in html
    assert "トークン（入力 / 出力 / 合計）" in html
