"""ニュース収集・初回起動確認・既読管理・履歴取得のJSON API Controller。

目的（Why）:
    初回起動時の件数確認・「はい/いいえ/キャンセル」選択、手動更新、カテゴリ変更時の再選別、
    既読状態の切替、および過去実行履歴取得のHTTPリクエストを受け付ける（指示書 §4.1, §6, §7, §8, §17, §18）。
仕様（What）:
    - `POST /api/onboarding/preview`: 対象日・カテゴリ指定による一時プレビュー生成
    - `POST /api/onboarding/decision`: 初回確認の「はい / いいえ / キャンセル」処理
    - `POST /api/news/refresh`: 手動ニュース更新
    - `POST /api/news/recategorize`: 保存済み記事を再利用したカテゴリ再選別
    - `POST /api/articles/{article_id}/read`: 既読・未読の切替
    - `GET /api/runs` / `GET /api/runs/{run_id}`: 過去実行履歴の取得
前提条件・依存関係（Assumption / Dependencies）:
    fastapi>=0.115, sqlalchemy>=2.0, `app.services.digest_orchestrator`, `app.schemas`
入出力（I/O）:
    入力: Pydanticスキーマで検証されたJSONリクエスト
    出力: 検証・整形済みのJSONレスポンス
注意点（Caution）:
    Controller 内で直接 `feedparser` や LLM SDK を呼び出してはならない（指示書 §4.1）。
今後の拡張ポイント（Future Work）:
    保留中AI処理（`ai_pending`）の手動再開エンドポイントの拡張。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 2-3）
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import DEFAULT_SELECTED_CATEGORIES
from app.core.database import get_db
from app.core.security import is_local_or_private_client
from app.repositories.article_repo import ArticleRepository
from app.repositories.run_repo import RunRepository
from app.schemas.article_schema import (
    OnboardingDecisionRequestSchema,
    OnboardingPreviewRequestSchema,
    OnboardingPreviewResponseSchema,
)
from app.services.digest_orchestrator import (
    build_view_data_for_run,
    execute_digest_run,
    handle_onboarding_decision,
    prepare_onboarding_preview,
    recategorize_from_saved_articles,
    resume_pending_ai_run,
)
from app.services.rss_service import format_datetime_jst

# =============================================================================
# 設定値ブロック
# =============================================================================

router = APIRouter(prefix="/api", tags=["news"])


class ManualRefreshRequest(BaseModel):
    """手動ニュース更新リクエストスキーマ。"""

    target_date: date | None = None
    selected_categories: list[str] | None = None


class RecategorizeRequest(BaseModel):
    """カテゴリ変更・表示件数変更・並び替えリクエストスキーマ。"""

    selected_categories: list[str] = Field(
        default_factory=lambda: list(DEFAULT_SELECTED_CATEGORIES),
        min_length=1,
    )
    display_count: int | None = Field(default=None, ge=1, le=15)
    sort_order: str | None = None


class ReadStatusRequest(BaseModel):
    """既読・未読更新リクエストスキーマ。"""

    is_read: bool = True


class RetryPendingRequest(BaseModel):
    """保留中AI処理の再開リクエストスキーマ。"""

    run_id: str | None = None


def _ensure_local_client(request: Request) -> None:
    """リクエスト元がローカルPCまたは同一Wi-Fi（プライベートLAN）内か確認する（指示書 §21）。"""
    host = request.client.host if request.client else "localhost"
    if not is_local_or_private_client(host):
        raise HTTPException(
            status_code=403,
            detail="ローカルネットワーク外からの操作は許可されていません。",
        )


@router.post("/onboarding/preview", response_model=OnboardingPreviewResponseSchema)
def api_onboarding_preview(
    payload: OnboardingPreviewRequestSchema,
    request: Request,
    db: Session = Depends(get_db),
) -> OnboardingPreviewResponseSchema:
    """初回起動時に対象日・カテゴリのニュースを一時収集し暫定件数と注意事項を返す（指示書 §6）。"""
    _ensure_local_client(request)
    return prepare_onboarding_preview(
        session=db,
        target_date=payload.target_date,
        selected_categories=payload.selected_categories,
        select_all=payload.select_all,
    )


@router.post("/onboarding/decision")
def api_onboarding_decision(
    payload: OnboardingDecisionRequestSchema,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """初回確認の「はい / いいえ / キャンセル」選択結果を処理する（指示書 §6.3）。"""
    _ensure_local_client(request)
    return handle_onboarding_decision(
        session=db,
        preview_token=payload.preview_token,
        decision=payload.decision,
        selected_categories=payload.selected_categories,
    )


@router.post("/news/refresh")
def api_manual_refresh(
    payload: ManualRefreshRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """手動でRSS新着・更新記事を取得し重要ニュースを選定する（指示書 §17.1）。"""
    _ensure_local_client(request)
    result = execute_digest_run(
        session=db,
        run_type="manual",
        target_date=payload.target_date,
        selected_categories=payload.selected_categories,
    )
    view_data = build_view_data_for_run(session=db, run_id=result.get("run_id"))
    return {
        "run_result": result,
        "cards": view_data["cards"],
        "summary": view_data["summary"],
    }


@router.post("/news/recategorize")
def api_recategorize(
    payload: RecategorizeRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """RSSを再取得せず保存済み記事を再利用してカテゴリ再選別を行う（指示書 §8）。"""
    _ensure_local_client(request)
    result = recategorize_from_saved_articles(
        session=db,
        selected_categories=payload.selected_categories,
        display_count=payload.display_count,
        sort_order=payload.sort_order,
    )
    view_data = build_view_data_for_run(session=db, run_id=result.get("run_id"))
    return {
        "run_result": result,
        "cards": view_data["cards"],
        "summary": view_data["summary"],
    }


@router.post("/news/retry-pending")
def api_retry_pending_ai(
    payload: RetryPendingRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """保留（`ai_pending`）となっている実行のAI分類・要約処理を再開する（指示書 §12.3, §14, §20）。"""
    _ensure_local_client(request)
    result = resume_pending_ai_run(
        session=db,
        run_id=payload.run_id,
    )
    view_data = build_view_data_for_run(session=db, run_id=result.get("run_id"))
    return {
        "run_result": result,
        "cards": view_data["cards"],
        "summary": view_data["summary"],
    }


@router.post("/articles/{article_id}/read")
def api_set_article_read(
    article_id: int,
    payload: ReadStatusRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """記事の既読・未読状態を更新する（指示書 §17.2）。"""
    _ensure_local_client(request)
    repo = ArticleRepository(db)
    updated = repo.set_read_status(article_id=article_id, is_read=payload.is_read)
    if updated is None:
        raise HTTPException(status_code=404, detail="対象の記事が見つかりません。")
    return {"id": updated.id, "is_read": updated.is_read}


@router.get("/runs")
def api_list_runs(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    """過去のニュース処理実行履歴一覧を取得する（指示書 §18）。"""
    repo = RunRepository(db)
    runs = repo.list_runs()
    return [
        {
            "run_id": r.run_id,
            "parent_run_id": r.parent_run_id,
            "run_type": r.run_type,
            "started_at_jst_str": format_datetime_jst(r.started_at),
            "status": r.status,
            "fetched_count": r.fetched_count,
            "deduped_count": r.deduped_count,
            "displayed_count": r.displayed_count,
            "reused_analysis": r.reused_analysis,
        }
        for r in runs
    ]


@router.get("/runs/{run_id}")
def api_get_run_detail(run_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """指定 `run_id` の選定ニュース一覧・サマリー・参考料金を取得する（指示書 §18）。"""
    repo = RunRepository(db)
    run_obj = repo.get_run(run_id)
    if run_obj is None:
        raise HTTPException(status_code=404, detail="指定された run_id が見つかりません。")
    view_data = build_view_data_for_run(session=db, run_id=run_id)
    return {
        "run_id": run_id,
        "cards": view_data["cards"],
        "summary": view_data["summary"],
    }


def validate_input(data: dict[str, Any]) -> dict[str, Any]:
    """入力辞書を検証する。"""
    return dict(data)


def execute_logic(validated: dict[str, Any]) -> dict[str, Any]:
    """ルーター情報を返す。"""
    return {"prefix": router.prefix, "routes_count": len(router.routes)}


def format_output(result: dict[str, Any]) -> dict[str, Any]:
    """結果辞書を整形して返す。"""
    return dict(result)


def main(data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Controller構成情報を返す。"""
    validated = validate_input(data or {})
    res = execute_logic(validated)
    return format_output(res)
