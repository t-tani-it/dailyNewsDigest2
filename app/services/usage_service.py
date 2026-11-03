"""1実行単位（`run_id`）のトークン使用量記録・集計サービス。

目的（Why）:
    ニュースカード単位ではなく「1回のニュース取得・選定・要約実行（`run_id`）」単位で、
    分類・重要度判定・要約に使われたすべてのAPIリクエストのトークン使用量を合算し、
    未計測（`None`）と `0` の区別、および推論トークンの二重計上防止を実現する
    （指示書 §14, §15, §17.3, §18）。
仕様（What）:
    - `aggregate_run_token_usage`: 指定 `run_id` の全 `LlmUsageLog` を走査し、入力・出力・推論・合計トークンと計測状態を集計
    - `get_usage_dashboard_data`: 当日呼び出し回数・モデル別集計・自動切替履歴を返すダッシュボード用集計
前提条件・依存関係（Assumption / Dependencies）:
    sqlalchemy>=2.0, `app.models.usage_log`, `app.services.model_router_service`, `app.services.rss_service`
入出力（I/O）:
    入力: SQLAlchemy `Session`, `run_id`
    出力: 1実行単位のトークン集計辞書およびダッシュボード集計辞書
注意点（Caution）:
    - APIからトークン数が返却されなかった（`unmeasured`）実行を `0 tokens` として表示してはならない（指示書 §15.1）。
    - `reasoning_included_in_output == True` のログでは、出力トークンに推論トークンが既に含まれているため
      合計トークン算出時に推論トークンを二重加算しないこと。
今後の拡張ポイント（Future Work）:
    月次トークン消費推移グラフ用データの出力。
変更履歴（Change Log）:
    2026-11-03 - 初版作成（Step 4-4）
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.rss_source import utc_now
from app.models.run import NewsRun
from app.models.usage_log import (
    USAGE_STATUS_FAILED,
    USAGE_STATUS_UNMEASURED,
    LlmUsageLog,
)
from app.services.model_router_service import check_daily_call_limit
from app.services.rss_service import format_datetime_jst

# =============================================================================
# 設定値ブロック（計測状態識別子・表示ラベル・取得上限）
# =============================================================================

MEASUREMENT_STATE_UNUSED: str = "unused"
MEASUREMENT_STATE_REUSED: str = "reused"
MEASUREMENT_STATE_MEASURED: str = "measured"
MEASUREMENT_STATE_PARTIAL: str = "partial_unmeasured"
MEASUREMENT_STATE_UNMEASURED: str = "unmeasured"

DEFAULT_RECENT_LOG_LIMIT: int = 100
DEFAULT_RECENT_RUN_LIMIT: int = 10


def compute_single_log_safe_total(log: LlmUsageLog) -> int | None:
    """1件の `LlmUsageLog` から推論トークン二重計上を防いだ合計トークン数を算出する。

    解説：
        - 入力または出力トークンが `None` の場合は未計測のため `None` を返す。
        - `reasoning_included_in_output == True` の場合は `input_tokens + output_tokens` を返し、
          `reasoning_tokens` を二重に足さない。
        - `reasoning_included_in_output == False` の場合のみ `reasoning_tokens` を加算する。

    Args:
        log (LlmUsageLog): 利用履歴レコード。

    Returns:
        int | None: 安全に計算された合計トークン数（未計測時は None）。

    Side Effects:
        なし。

    Examples:
        >>> l = LlmUsageLog(run_id="r1", provider="openrouter", model_id="openrouter/free", input_tokens=100, output_tokens=50, reasoning_tokens=20, reasoning_included_in_output=True)
        >>> compute_single_log_safe_total(l)
        150
    """
    if log.input_tokens is None or log.output_tokens is None:
        return None
    in_val = max(0, int(log.input_tokens))
    out_val = max(0, int(log.output_tokens))
    reas_val = max(0, int(log.reasoning_tokens)) if log.reasoning_tokens is not None else 0

    if not log.reasoning_included_in_output and reas_val > 0:
        return in_val + out_val + reas_val
    return in_val + out_val


def aggregate_run_token_usage(
    session: Session,
    run_id: str,
    reused_analysis: bool = False,
) -> dict[str, Any]:
    """指定 `run_id` に紐づく全API呼び出しのトークン使用量を集計する（指示書 §14, §15）。

    解説：
        1. `llm_usage_logs` から `run_id` 一致レコードを時系列順で取得する。
        2. レコードが0件の場合：
           - 既存分析再利用（`reused_analysis=True`）なら新規トークン消費 0 として返す。
           - それ以外は LLM未使用（0）として返す。
        3. レコードが1件以上ある場合：
           - トークン数が計測できた呼び出しと、未計測（`None`）の呼び出しを分けて集計する。
           - 1件も計測値が存在しない場合は、0 と誤表示せず `total_input_tokens=None`,
             `total_output_tokens=None`, `total_tokens=None`, `measurement_state="unmeasured"` を返す。
           - 一部のみ計測できた場合は `measurement_state="partial_unmeasured"` とし、
             計測済み分の合計値と注意書きを返す。

    Args:
        session (Session): SQLAlchemy セッション。
        run_id (str): 集計対象の `run_id`。
        reused_analysis (bool): 保存済み分析を再利用した実行かどうか。

    Returns:
        dict[str, Any]: トークン合計・リクエスト数・使用モデル・計測状態・切替履歴を含む辞書。

    Side Effects:
        なし。
    """
    stmt = (
        select(LlmUsageLog)
        .where(LlmUsageLog.run_id == run_id)
        .order_by(LlmUsageLog.called_at.asc(), LlmUsageLog.id.asc())
    )
    logs = list(session.scalars(stmt).all())

    if not logs:
        if reused_analysis:
            return {
                "api_request_count": 0,
                "measured_request_count": 0,
                "unmeasured_request_count": 0,
                "failed_request_count": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_reasoning_tokens": 0,
                "total_tokens": 0,
                "measurement_state": MEASUREMENT_STATE_REUSED,
                "usage_measurement_status": "既存分析再利用（新規トークン消費0）",
                "used_provider": None,
                "used_model_id": None,
                "switch_events": [],
            }
        return {
            "api_request_count": 0,
            "measured_request_count": 0,
            "unmeasured_request_count": 0,
            "failed_request_count": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_reasoning_tokens": 0,
            "total_tokens": 0,
            "measurement_state": MEASUREMENT_STATE_UNUSED,
            "usage_measurement_status": "LLM未使用",
            "used_provider": None,
            "used_model_id": None,
            "switch_events": [],
        }

    sum_input = 0
    sum_output = 0
    sum_reasoning = 0
    sum_total = 0
    has_reasoning = False

    measured_count = 0
    unmeasured_count = 0
    failed_count = 0

    used_providers: list[str] = []
    used_models: list[str] = []
    switch_events: list[dict[str, Any]] = []

    for log in logs:
        if log.switched_from_model:
            switch_events.append(
                {
                    "from_model": log.switched_from_model,
                    "to_model": log.model_id,
                    "reason": log.switch_reason or "unknown",
                    "purpose": log.purpose,
                }
            )

        if log.usage_status == "failed" and log.input_tokens is None and log.output_tokens is None:
            failed_count += 1
            continue

        if log.provider and log.provider not in used_providers:
            used_providers.append(log.provider)
        if log.model_id and log.model_id not in used_models:
            used_models.append(log.model_id)

        safe_total = compute_single_log_safe_total(log)
        if (
            safe_total is not None
            and log.input_tokens is not None
            and log.output_tokens is not None
            and log.usage_status != USAGE_STATUS_UNMEASURED
        ):
            measured_count += 1
            sum_input += max(0, int(log.input_tokens))
            sum_output += max(0, int(log.output_tokens))
            if log.reasoning_tokens is not None:
                has_reasoning = True
                sum_reasoning += max(0, int(log.reasoning_tokens))
            sum_total += safe_total
        else:
            unmeasured_count += 1

    provider_str = " / ".join(used_providers) if used_providers else None
    model_str = " / ".join(used_models) if used_models else None

    if measured_count == 0:
        # 1件もトークン数が取得できなかった場合：0と誤表示せず None（未計測）を返す（指示書 §15.1）
        return {
            "api_request_count": len(logs),
            "measured_request_count": 0,
            "unmeasured_request_count": unmeasured_count,
            "failed_request_count": failed_count,
            "total_input_tokens": None,
            "total_output_tokens": None,
            "total_reasoning_tokens": None,
            "total_tokens": None,
            "measurement_state": MEASUREMENT_STATE_UNMEASURED,
            "usage_measurement_status": "未計測（APIからトークン数が返却されませんでした）",
            "used_provider": provider_str,
            "used_model_id": model_str,
            "switch_events": switch_events,
        }

    if unmeasured_count > 0:
        state = MEASUREMENT_STATE_PARTIAL
        status_text = (
            f"一部未計測あり（計測済み {measured_count}回 / 未計測 {unmeasured_count}回）"
        )
    else:
        state = MEASUREMENT_STATE_MEASURED
        status_text = f"計測済み（APIリクエスト {measured_count}回）"

    return {
        "api_request_count": len(logs),
        "measured_request_count": measured_count,
        "unmeasured_request_count": unmeasured_count,
        "failed_request_count": failed_count,
        "total_input_tokens": sum_input,
        "total_output_tokens": sum_output,
        "total_reasoning_tokens": sum_reasoning if has_reasoning else 0,
        "total_tokens": sum_total,
        "measurement_state": state,
        "usage_measurement_status": status_text,
        "used_provider": provider_str,
        "used_model_id": model_str,
        "switch_events": switch_events,
    }


def summarize_usage_logs(logs: list[LlmUsageLog]) -> dict[str, Any]:
    """利用履歴リスト全体の合計・計測状態・切替件数を集計する。

    解説：
        - 失敗かつトークン未取得のリクエストは件数のみ数え、トークン合計には含めない。
        - 計測済みが1件も無い場合は `None`（未計測）を返し、`0 tokens` と誤表示しない。
        - 推論トークンは `compute_single_log_safe_total` と同じ規則で二重計上しない。

    Args:
        logs (list[LlmUsageLog]): 集計対象の利用履歴リスト。

    Returns:
        dict[str, Any]: 合計トークン・成功/失敗件数・計測状態・切替件数を含む辞書。

    Side Effects:
        なし。

    Examples:
        >>> summarize_usage_logs([])["measurement_state"]
        'unused'
    """
    if not logs:
        return {
            "total_calls": 0,
            "success_count": 0,
            "failed_count": 0,
            "measured_count": 0,
            "unmeasured_count": 0,
            "switch_count": 0,
            "model_count": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_reasoning_tokens": 0,
            "total_tokens": 0,
            "measurement_state": MEASUREMENT_STATE_UNUSED,
            "measurement_label": "LLM未使用",
        }

    success_count = 0
    failed_count = 0
    measured_count = 0
    unmeasured_count = 0
    switch_count = 0
    sum_input = 0
    sum_output = 0
    sum_reasoning = 0
    sum_total = 0
    has_reasoning = False
    model_keys: set[str] = set()

    for log in logs:
        if log.provider and log.model_id:
            model_keys.add(f"{log.provider}:{log.model_id}")
        if log.switched_from_model:
            switch_count += 1

        is_failed = log.usage_status == USAGE_STATUS_FAILED
        if is_failed:
            failed_count += 1
        else:
            success_count += 1

        # 失敗かつトークン未取得の呼び出しは、トークン合計の分母に含めない
        if is_failed and log.input_tokens is None and log.output_tokens is None:
            continue

        safe_total = compute_single_log_safe_total(log)
        if (
            safe_total is not None
            and log.input_tokens is not None
            and log.output_tokens is not None
            and log.usage_status != USAGE_STATUS_UNMEASURED
        ):
            measured_count += 1
            sum_input += max(0, int(log.input_tokens))
            sum_output += max(0, int(log.output_tokens))
            if log.reasoning_tokens is not None:
                has_reasoning = True
                sum_reasoning += max(0, int(log.reasoning_tokens))
            sum_total += safe_total
        else:
            unmeasured_count += 1

    if measured_count == 0:
        measurement_state = MEASUREMENT_STATE_UNMEASURED
        measurement_label = "未計測（APIからトークン数が返却されませんでした）"
        total_in: int | None = None
        total_out: int | None = None
        total_reasoning: int | None = None
        total_all: int | None = None
    elif unmeasured_count > 0:
        measurement_state = MEASUREMENT_STATE_PARTIAL
        measurement_label = (
            f"一部未計測あり（計測済み {measured_count}回 / 未計測 {unmeasured_count}回）"
        )
        total_in = sum_input
        total_out = sum_output
        total_reasoning = sum_reasoning if has_reasoning else 0
        total_all = sum_total
    else:
        measurement_state = MEASUREMENT_STATE_MEASURED
        measurement_label = f"計測済み（APIリクエスト {measured_count}回）"
        total_in = sum_input
        total_out = sum_output
        total_reasoning = sum_reasoning if has_reasoning else 0
        total_all = sum_total

    return {
        "total_calls": len(logs),
        "success_count": success_count,
        "failed_count": failed_count,
        "measured_count": measured_count,
        "unmeasured_count": unmeasured_count,
        "switch_count": switch_count,
        "model_count": len(model_keys),
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "total_reasoning_tokens": total_reasoning,
        "total_tokens": total_all,
        "measurement_state": measurement_state,
        "measurement_label": measurement_label,
    }


def list_recent_run_usage(
    session: Session,
    limit_runs: int = DEFAULT_RECENT_RUN_LIMIT,
) -> list[dict[str, Any]]:
    """直近の実行（`NewsRun`）ごとにトークン使用量を集計して返す（指示書 §17.3, §18）。

    Args:
        session (Session): SQLAlchemy セッション。
        limit_runs (int): 取得する実行件数（1以上）。

    Returns:
        list[dict[str, Any]]: 実行情報とトークン集計をまとめた辞書リスト（新しい順）。

    Side Effects:
        なし。
    """
    stmt = (
        select(NewsRun)
        .order_by(NewsRun.started_at.desc())
        .limit(max(1, int(limit_runs)))
    )
    runs = list(session.scalars(stmt).all())

    rows: list[dict[str, Any]] = []
    for run_obj in runs:
        usage = aggregate_run_token_usage(
            session=session,
            run_id=run_obj.run_id,
            reused_analysis=bool(run_obj.reused_analysis),
        )
        rows.append(
            {
                "run_id": run_obj.run_id,
                "parent_run_id": run_obj.parent_run_id,
                "executed_at_jst_str": format_datetime_jst(run_obj.started_at),
                "run_type": run_obj.run_type,
                "status": run_obj.status,
                "reused_analysis": bool(run_obj.reused_analysis),
                "ai_classified_count": int(run_obj.ai_classified_count or 0),
                "ai_summarized_count": int(run_obj.ai_summarized_count or 0),
                "displayed_count": int(run_obj.displayed_count or 0),
                "used_provider": run_obj.used_provider or usage["used_provider"],
                "used_model_id": run_obj.used_model_id or usage["used_model_id"],
                "api_request_count": usage["api_request_count"],
                "total_input_tokens": usage["total_input_tokens"],
                "total_output_tokens": usage["total_output_tokens"],
                "total_tokens": usage["total_tokens"],
                "measurement_state": usage["measurement_state"],
                "usage_measurement_status": usage["usage_measurement_status"],
            }
        )
    return rows


def get_usage_dashboard_data(
    session: Session,
    limit_logs: int = DEFAULT_RECENT_LOG_LIMIT,
    limit_runs: int = DEFAULT_RECENT_RUN_LIMIT,
) -> dict[str, Any]:
    """使用量ダッシュボード向けに当日の上限状況・モデル別集計・自動切替履歴を返す。

    Args:
        session (Session): SQLAlchemy セッション。
        limit_logs (int): 直近の呼び出しログ取得上限。
        limit_runs (int): 直近の実行履歴取得上限。

    Returns:
        dict[str, Any]: `daily_limit_status`, `overview`, `model_breakdown`, `recent_logs`,
        `recent_run_usages`, `switch_history`。

    Side Effects:
        なし。
    """
    daily_status = check_daily_call_limit(session)
    stmt = (
        select(LlmUsageLog)
        .order_by(LlmUsageLog.called_at.desc(), LlmUsageLog.id.desc())
        .limit(int(limit_logs))
    )
    recent_logs = list(session.scalars(stmt).all())

    by_model: dict[str, dict[str, Any]] = {}
    switch_history: list[dict[str, Any]] = []

    for log in recent_logs:
        key = f"{log.provider}:{log.model_id}"
        if key not in by_model:
            by_model[key] = {
                "provider": log.provider,
                "model_id": log.model_id,
                "call_count": 0,
                "success_count": 0,
                "failed_count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "measured_count": 0,
                "unmeasured_count": 0,
                "has_unmeasured": False,
            }
        entry = by_model[key]
        entry["call_count"] += 1
        if log.usage_status == USAGE_STATUS_FAILED:
            entry["failed_count"] += 1
        else:
            entry["success_count"] += 1

        safe_tot = compute_single_log_safe_total(log)
        if (
            safe_tot is not None
            and log.input_tokens is not None
            and log.output_tokens is not None
            and log.usage_status != USAGE_STATUS_UNMEASURED
        ):
            entry["measured_count"] += 1
            entry["input_tokens"] += int(log.input_tokens)
            entry["output_tokens"] += int(log.output_tokens)
            entry["total_tokens"] += safe_tot
        else:
            entry["unmeasured_count"] += 1
            entry["has_unmeasured"] = True

        if log.switched_from_model:
            switch_history.append(
                {
                    "called_at_jst_str": format_datetime_jst(log.called_at),
                    "run_id": log.run_id,
                    "purpose": log.purpose,
                    "from_model": log.switched_from_model,
                    "to_model": log.model_id,
                    "reason": log.switch_reason or "unknown",
                }
            )

    return {
        "generated_at_jst_str": format_datetime_jst(utc_now()),
        "daily_limit_status": daily_status,
        "overview": summarize_usage_logs(recent_logs),
        "model_breakdown": list(by_model.values()),
        "switch_history": switch_history,
        "recent_run_usages": list_recent_run_usage(session, limit_runs=limit_runs),
        "recent_logs": [
            {
                "call_id": log_row.call_id,
                "run_id": log_row.run_id,
                "called_at_jst_str": format_datetime_jst(log_row.called_at),
                "purpose": log_row.purpose,
                "provider": log_row.provider,
                "model_id": log_row.model_id,
                "input_tokens": log_row.input_tokens,
                "output_tokens": log_row.output_tokens,
                "reasoning_tokens": log_row.reasoning_tokens,
                "total_tokens": log_row.total_tokens,
                "usage_status": log_row.usage_status,
                "http_status": log_row.http_status,
                "switched_from_model": log_row.switched_from_model,
                "switch_reason": log_row.switch_reason,
                "error_message": log_row.error_message,
            }
            for log_row in recent_logs
        ],
    }


def validate_input(data: dict[str, Any]) -> dict[str, Any]:
    """使用量サービスの入力辞書を検証する。"""
    if not isinstance(data, dict) or not isinstance(data.get("session"), Session):
        raise ValueError("有効な SQLAlchemy Session が必要です。")
    return dict(data)


def execute_logic(validated: dict[str, Any]) -> dict[str, Any]:
    """指定されたアクション（`run_summary` または `dashboard`）を実行する。"""
    session: Session = validated["session"]
    action = str(validated.get("action", "run_summary"))
    if action == "dashboard":
        return get_usage_dashboard_data(
            session=session,
            limit_logs=int(validated.get("limit_logs", 100)),
        )
    run_id = str(validated.get("run_id") or "")
    if not run_id:
        raise ValueError("run_summary には run_id が必須です。")
    return aggregate_run_token_usage(
        session=session,
        run_id=run_id,
        reused_analysis=bool(validated.get("reused_analysis", False)),
    )


def format_output(result: dict[str, Any]) -> dict[str, Any]:
    """結果辞書を整形して返す。"""
    return dict(result)


def main(data: dict[str, Any]) -> dict[str, Any]:
    """トークン使用量集計の一連の処理を実行する。"""
    validated = validate_input(data)
    res = execute_logic(validated)
    return format_output(res)
