"""AI使用量ダッシュボードのJSON API Controller。

目的（Why）:
    ブラウザや外部のローカルツールから、トークン使用量・モデル別呼び出し回数・
    自動切替履歴・比較対象有料3モデルの単価確認状態をJSONで取得できるようにする
    （指示書 §14, §15, §16, §17.3, §18）。
仕様（What）:
    - `GET /api/usage`: 使用量ダッシュボードの集計JSONを返す
    - クエリ `limit_logs` / `limit_runs` で取得件数を制限
    - 任意の `run_id` 指定時は、その実行のトークン集計も併記
前提条件・依存関係（Assumption / Dependencies）:
    fastapi>=0.115, sqlalchemy>=2.0, `app.services.usage_dashboard_service`
入出力（I/O）:
    入力: クエリパラメータ（件数上限・任意の `run_id`）
    出力: ダッシュボード集計JSON
注意点（Caution）:
    比較対象有料3モデルは参考料金計算専用であり、本APIからAPI呼び出しは行わない。
今後の拡張ポイント（Future Work）:
    期間指定（開始日・終了日）でのトークン推移集計。
変更履歴（Change Log）:
    2026-11-03 - 初版作成（Step 5-2）
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.repositories.run_repo import RunRepository
from app.services.usage_dashboard_service import build_usage_dashboard

# =============================================================================
# 設定値ブロック（クエリ上限）
# =============================================================================

router = APIRouter(prefix="/api", tags=["usage"])

MAX_LOG_LIMIT: int = 500
MAX_RUN_LIMIT: int = 50


@router.get("/usage")
def api_get_usage(
    limit_logs: int = Query(default=100, ge=1, le=MAX_LOG_LIMIT),
    limit_runs: int = Query(default=10, ge=1, le=MAX_RUN_LIMIT),
    run_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """使用量ダッシュボードの集計データを返す。"""
    # 存在しない run_id を渡された場合に「LLM未使用」と誤って返さない
    if run_id and RunRepository(db).get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="指定された実行履歴が見つかりません。")
    return build_usage_dashboard(
        session=db,
        limit_logs=limit_logs,
        limit_runs=limit_runs,
        run_id=run_id,
    )
