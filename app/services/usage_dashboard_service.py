"""AI使用量ダッシュボード・過去実行履歴向けの集約サービス。

目的（Why）:
    トークン使用量、モデル別呼び出し回数、自動切替履歴、比較対象有料3モデルの
    単価確認状態を1画面に集約し、無料運用の実績と参考料金の根拠を追跡できるようにする
    （指示書 §14, §15, §16, §17.3, §18）。
仕様（What）:
    - `build_usage_dashboard`: 使用量ダッシュボード用データ（サマリー・モデル別・実行別・参考単価）を構築
    - `build_run_usage_map`: 過去実行履歴の一覧行に表示する `run_id` 別トークン集計マップを構築
前提条件・依存関係（Assumption / Dependencies）:
    sqlalchemy>=2.0, `app.services.usage_service`, `app.services.pricing_service`
入出力（I/O）:
    入力: SQLAlchemy `Session`、取得件数上限、任意の `run_id`
    出力: Jinja2テンプレート / JSON API へ渡す辞書
注意点（Caution）:
    - 比較対象有料3モデルは参考料金計算専用であり、API呼び出しは行わない。
    - 未計測（`None`）を `0` と混同しない。テンプレート側でも `None` 判定を行う。
今後の拡張ポイント（Future Work）:
    月次・週次のトークン推移グラフ用データの追加。
変更履歴（Change Log）:
    2026-11-03 - 初版作成（Step 5-2）
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.run import NewsRun
from app.schemas.run_schema import REFERENCE_PRICE_DISCLAIMER
from app.services.pricing_service import get_price_snapshot_rows
from app.services.usage_service import (
    DEFAULT_RECENT_LOG_LIMIT,
    DEFAULT_RECENT_RUN_LIMIT,
    aggregate_run_token_usage,
    get_usage_dashboard_data,
)

# =============================================================================
# 設定値ブロック（ダッシュボード取得上限）
# =============================================================================

DASHBOARD_DEFAULT_LOG_LIMIT: int = DEFAULT_RECENT_LOG_LIMIT
DASHBOARD_DEFAULT_RUN_LIMIT: int = DEFAULT_RECENT_RUN_LIMIT


def build_usage_dashboard(
    session: Session,
    limit_logs: int = DASHBOARD_DEFAULT_LOG_LIMIT,
    limit_runs: int = DASHBOARD_DEFAULT_RUN_LIMIT,
    run_id: str | None = None,
) -> dict[str, Any]:
    """使用量ダッシュボードに必要な全データを組み立てる。

    Args:
        session (Session): SQLAlchemy セッション。
        limit_logs (int): 直近のAPI呼び出しログ取得上限。
        limit_runs (int): 直近の実行履歴取得上限。
        run_id (str | None): 個別実行の詳細を併記する場合の `run_id`。

    Returns:
        dict[str, Any]: 利用状況・モデル別集計・実行別集計・自動切替履歴・参考単価・注意書き。

    Side Effects:
        初期モデルカタログ／価格スナップショット未登録時にシードを実行する。

    Examples:
        >>> data = build_usage_dashboard(session)
        >>> "overview" in data
        True
    """
    dashboard = get_usage_dashboard_data(
        session=session,
        limit_logs=max(1, int(limit_logs)),
        limit_runs=max(1, int(limit_runs)),
    )
    dashboard["price_snapshots"] = get_price_snapshot_rows(session)
    dashboard["disclaimer"] = REFERENCE_PRICE_DISCLAIMER
    dashboard["log_limit"] = max(1, int(limit_logs))
    dashboard["run_limit"] = max(1, int(limit_runs))

    if run_id:
        dashboard["selected_run_usage"] = aggregate_run_token_usage(
            session=session,
            run_id=run_id,
            reused_analysis=False,
        )
        dashboard["selected_run_id"] = run_id
    else:
        dashboard["selected_run_usage"] = None
        dashboard["selected_run_id"] = None

    return dashboard


def build_run_usage_map(
    session: Session,
    runs: list[NewsRun],
) -> dict[str, dict[str, Any]]:
    """過去実行履歴の一覧表示用に `run_id` ごとのトークン集計マップを作る。

    Args:
        session (Session): SQLAlchemy セッション。
        runs (list[NewsRun]): 履歴一覧に表示する実行リスト。

    Returns:
        dict[str, dict[str, Any]]: `run_id` をキーとしたトークン集計辞書。

    Side Effects:
        なし。

    Examples:
        >>> usage_map = build_run_usage_map(session, runs)
        >>> runs[0].run_id in usage_map
        True
    """
    usage_map: dict[str, dict[str, Any]] = {}
    for run_obj in runs:
        usage_map[run_obj.run_id] = aggregate_run_token_usage(
            session=session,
            run_id=run_obj.run_id,
            reused_analysis=bool(run_obj.reused_analysis),
        )
    return usage_map


def validate_input(data: dict[str, Any]) -> dict[str, Any]:
    """ダッシュボードサービスの入力辞書を検証する。"""
    if not isinstance(data, dict) or not isinstance(data.get("session"), Session):
        raise ValueError("有効な SQLAlchemy Session が必要です。")
    return dict(data)


def execute_logic(validated: dict[str, Any]) -> dict[str, Any]:
    """指定されたアクション（既定はダッシュボード構築）を実行する。"""
    return build_usage_dashboard(
        session=validated["session"],
        limit_logs=int(validated.get("limit_logs", DASHBOARD_DEFAULT_LOG_LIMIT)),
        limit_runs=int(validated.get("limit_runs", DASHBOARD_DEFAULT_RUN_LIMIT)),
        run_id=validated.get("run_id"),
    )


def format_output(result: dict[str, Any]) -> dict[str, Any]:
    """結果辞書をそのまま返す。"""
    return dict(result)


def main(data: dict[str, Any]) -> dict[str, Any]:
    """使用量ダッシュボードの構築を一括実行する。"""
    validated = validate_input(data)
    result = execute_logic(validated)
    return format_output(result)
