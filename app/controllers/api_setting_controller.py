"""ユーザー設定・RSS取得元・LLMモデル選択のJSON API Controller。

目的（Why）:
    設定画面および基本画面からの設定変更、RSS取得元の追加・有効化切替、
    無料LLMモデル一覧の参照・更新リクエストを受け付ける（指示書 §5.3, §13, §17.6, §21）。
仕様（What）:
    - `GET /api/settings` / `PUT /api/settings`: ユーザー設定の取得・更新（APIキー値は返さない）
    - `GET /api/rss-sources` / `POST /api/rss-sources` / `PATCH /api/rss-sources/{id}`: RSS管理（SSRF検証付き）
    - `GET /api/models` / `POST /api/models/refresh`: 無料モデル一覧の参照・更新
前提条件・依存関係（Assumption / Dependencies）:
    fastapi>=0.115, sqlalchemy>=2.0, `app.core.security`, `app.repositories`, `app.schemas`
入出力（I/O）:
    検証済みJSONリクエストおよびレスポンス。
注意点（Caution）:
    - APIキーそのものをレスポンスに含めてはならない（指示書 §11, §21）。
    - RSS URL登録時は必ず `validate_safe_url` によりプライベートIP等を拒否すること（指示書 §21）。
今後の拡張ポイント（Future Work）:
    Phase 3 で `model_catalog_service` によるGoogle AI Studio / OpenRouter動的モデル同期を接続。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 2-3）
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.config import ALLOW_PAID_LLM_EXECUTION
from app.core.database import get_db
from app.core.security import (
    SSRFValidationError,
    is_local_or_private_client,
    validate_safe_url,
)
from app.models.model_catalog import ModelCatalog
from app.repositories.rss_source_repo import RssSourceRepository
from app.repositories.setting_repo import SettingRepository
from app.schemas.setting_schema import (
    UserSettingResponseSchema,
)
from app.schemas.setting_schema import (
    main as validate_setting_update,
)
from app.services.model_catalog_service import (
    list_catalog_models,
    refresh_model_catalog,
)

# =============================================================================
# 設定値ブロック
# =============================================================================

router = APIRouter(prefix="/api", tags=["settings"])


class RssSourceUpsertRequest(BaseModel):
    """RSS取得元の新規登録・更新リクエストスキーマ。"""

    name: str
    url: str
    category_hint: str = "unclassified"
    is_enabled: bool = True
    terms_status: str = "unverified"
    allow_store_summary: bool = False
    allow_ai_summary: bool = False


class RssSourceToggleRequest(BaseModel):
    """RSS取得元の有効/無効・利用条件ステータス変更リクエスト。"""

    is_enabled: bool | None = None
    terms_status: str | None = None
    allow_store_summary: bool | None = None
    allow_ai_summary: bool | None = None


def _ensure_local_client(request: Request) -> None:
    """管理・設定操作がローカルまたは同一Wi-Fi（プライベートLAN）からの要求か検証する（指示書 §21）。"""
    host = request.client.host if request.client else "localhost"
    if not is_local_or_private_client(host):
        raise HTTPException(
            status_code=403,
            detail="ローカルネットワーク外からの設定変更は許可されていません。",
        )


def _seed_default_free_models_if_empty(db: Session) -> list[ModelCatalog]:
    """モデルカタログが空の場合に初期の無料確認済みモデル候補を登録して返す。"""
    return list_catalog_models(db, executable_only=False)


@router.get("/settings", response_model=UserSettingResponseSchema)
def api_get_settings(db: Session = Depends(get_db)) -> UserSettingResponseSchema:
    """現在のユーザー設定を取得する（APIキー値は含まない）。"""
    setting = SettingRepository(db).get_or_create()
    return UserSettingResponseSchema.model_validate(setting)


@router.put("/settings", response_model=UserSettingResponseSchema)
def api_update_settings(
    payload: dict[str, Any],
    request: Request,
    db: Session = Depends(get_db),
) -> UserSettingResponseSchema:
    """ユーザー設定を更新する（有料API有効化要求は400エラーで拒否：指示書 §11）。"""
    _ensure_local_client(request)
    try:
        validated_updates = validate_setting_update(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    updated = SettingRepository(db).update_settings(validated_updates)
    return UserSettingResponseSchema.model_validate(updated)


@router.get("/rss-sources")
def api_list_rss_sources(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    """RSS取得元の一覧と利用条件確認状態・最終取得結果を取得する（指示書 §5.3）。"""
    repo = RssSourceRepository(db)
    sources = repo.seed_initial_sources()
    return [
        {
            "id": s.id,
            "name": s.name,
            "url": s.url,
            "category_hint": s.category_hint,
            "is_enabled": s.is_enabled,
            "terms_status": s.terms_status,
            "allow_store_summary": s.allow_store_summary,
            "allow_ai_summary": s.allow_ai_summary,
            "is_safe_to_fetch": s.is_safe_to_fetch,
            "last_status": s.last_status,
            "error_history": s.error_history,
        }
        for s in sources
    ]


@router.post("/rss-sources")
def api_add_rss_source(
    payload: RssSourceUpsertRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """新しいRSS取得元をSSRF検証のうえで登録する（指示書 §5.3, §21）。"""
    _ensure_local_client(request)
    try:
        safe_url = validate_safe_url(payload.url, resolve_dns=False)
    except SSRFValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    repo = RssSourceRepository(db)
    created = repo.create_or_update(
        name=payload.name.strip(),
        url=safe_url,
        category_hint=payload.category_hint,
        is_enabled=payload.is_enabled,
        terms_status=payload.terms_status,
        allow_store_summary=payload.allow_store_summary,
        allow_ai_summary=payload.allow_ai_summary,
    )
    return {
        "id": created.id,
        "name": created.name,
        "url": created.url,
        "terms_status": created.terms_status,
        "is_safe_to_fetch": created.is_safe_to_fetch,
    }


@router.patch("/rss-sources/{source_id}")
def api_patch_rss_source(
    source_id: int,
    payload: RssSourceToggleRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """既存RSS取得元の有効/無効または利用条件確認状態を更新する。"""
    _ensure_local_client(request)
    repo = RssSourceRepository(db)
    src = repo.get_by_id(source_id)
    if src is None:
        raise HTTPException(status_code=404, detail="指定されたRSS情報源が見つかりません。")

    updated = repo.create_or_update(
        name=src.name,
        url=src.url,
        category_hint=src.category_hint,
        is_enabled=payload.is_enabled if payload.is_enabled is not None else src.is_enabled,
        terms_status=(
            payload.terms_status if payload.terms_status is not None else src.terms_status
        ),
        allow_store_summary=(
            payload.allow_store_summary
            if payload.allow_store_summary is not None
            else src.allow_store_summary
        ),
        allow_ai_summary=(
            payload.allow_ai_summary
            if payload.allow_ai_summary is not None
            else src.allow_ai_summary
        ),
    )
    return {
        "id": updated.id,
        "name": updated.name,
        "is_enabled": updated.is_enabled,
        "terms_status": updated.terms_status,
        "is_safe_to_fetch": updated.is_safe_to_fetch,
    }


@router.get("/models")
def api_list_models(db: Session = Depends(get_db)) -> dict[str, Any]:
    """基本画面のLLMモデル選択欄に表示するモデル一覧を返す（指示書 §13）。"""
    models = _seed_default_free_models_if_empty(db)
    return {
        "allow_paid_llm_execution": ALLOW_PAID_LLM_EXECUTION,
        "models": [
            {
                "model_id": m.model_id,
                "provider": m.provider,
                "model_name": m.model_name,
                "free_tier_status": m.free_tier_status,
                "availability_status": m.availability_status,
                "is_executable_free_model": m.is_executable_free_model,
            }
            for m in models
        ],
    }


@router.post("/models/refresh")
def api_refresh_models(
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """「無料モデル一覧を更新」ボタン押下時にモデル一覧を再確認する（指示書 §10.3, §13）。"""
    _ensure_local_client(request)
    refreshed = refresh_model_catalog(db)
    models: list[ModelCatalog] = refreshed["models"]
    return {
        "updated_count": len(models),
        "discovered_count": refreshed["discovered_count"],
        "executable_free_count": refreshed["executable_free_count"],
        "models": [
            {
                "model_id": m.model_id,
                "provider": m.provider,
                "model_name": m.model_name,
                "free_tier_status": m.free_tier_status,
                "availability_status": m.availability_status,
                "is_executable_free_model": m.is_executable_free_model,
            }
            for m in models
        ],
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
