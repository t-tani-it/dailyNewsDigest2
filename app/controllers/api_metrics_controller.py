"""Phase 2 のRSS取得量計測レポート・計測実行のJSON API Controller。

目的（Why）:
    ブラウザから「現在の実測進捗」をJSONで取得し、また「RSS取得量の計測」を
    LLM呼び出しなしで実行できるようにする（指示書 §22 Phase 2、期間は2日へ読み替え）。
仕様（What）:
    - `GET /api/metrics`: 実測日数・暫定/完了判定・カテゴリ別件数・推定トークンを含むレポート
    - `POST /api/metrics/measure`: RSS収集のみを実行して日次計測値を保存
前提条件・依存関係（Assumption / Dependencies）:
    fastapi>=0.115, sqlalchemy>=2.0, `app.services.metrics_service`
入出力（I/O）:
    入力: 任意の対象日（JSON）
    出力: 計測レポートJSON
注意点（Caution）:
    計測実行はローカルまたは同一Wi-Fi内からの操作に限定する（指示書 §21）。
    計測専用実行ではLLMを呼び出さず、記事の本文保存も行わない。
今後の拡張ポイント（Future Work）:
    期間指定（開始日・終了日）でのレポート取得。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 3-2）
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import is_local_or_private_client
from app.services.metrics_service import get_phase2_report, run_rss_measurement

# =============================================================================
# 設定値ブロック
# =============================================================================

router = APIRouter(prefix="/api", tags=["metrics"])


class MetricsMeasureRequest(BaseModel):
    """RSS取得量計測リクエストスキーマ。"""

    target_date: date | None = None


def _ensure_local_client(request: Request) -> None:
    """計測実行がローカルまたは同一Wi-Fi（プライベートLAN）内からの要求か検証する。"""
    host = request.client.host if request.client else "localhost"
    if not is_local_or_private_client(host):
        raise HTTPException(
            status_code=403,
            detail="ローカルネットワーク外からの計測実行は許可されていません。",
        )


@router.get("/metrics")
def api_get_metrics(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Phase 2 の実測進捗・集計レポートを返す。"""
    return get_phase2_report(db)


@router.post("/metrics/measure")
def api_run_metrics_measurement(
    payload: MetricsMeasureRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """RSS収集のみを実行し、日次計測値を保存して最新レポートを返す。"""
    _ensure_local_client(request)
    return run_rss_measurement(
        session=db,
        target_date=payload.target_date,
    )
