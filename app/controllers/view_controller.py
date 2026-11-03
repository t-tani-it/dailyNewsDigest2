"""Jinja2 HTMLテンプレートを描画する画面表示 Controller。

目的（Why）:
    PC・スマートフォンの両方に対応する日本語Web画面（初回起動確認画面、基本画面、
    過去実行履歴画面、設定画面）へのデータ受け渡しを行う（指示書 §4.1, §6, §17, §18）。
仕様（What）:
    - `GET /`: 初回未承認時は `onboarding.html`、承認済み時は `index.html` を表示
    - `GET /onboarding`: 初回確認画面を明示表示
    - `GET /history`: 過去の `run_id` ごとのニュース一覧・サマリー・参考料金を表示
    - `GET /settings`: 設定画面を表示
    - `GET /metrics`: Phase 2 のRSS取得量計測レポートを表示
    - `GET /usage`: AI使用量・無料モデル運用ダッシュボードを表示
前提条件・依存関係（Assumption / Dependencies）:
    fastapi>=0.115, jinja2>=3.1, `app.services.digest_orchestrator`,
    `app.services.usage_dashboard_service`
入出力（I/O）:
    HTMLレスポンス（`TemplateResponse`）。
注意点（Caution）:
    テンプレートへAPIキーの生値を一切渡してはならない（指示書 §11, §21）。
今後の拡張ポイント（Future Work）:
    月次推移グラフの表示。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 2-3）
    2026-11-03 - 使用量ダッシュボード（`GET /usage`）と履歴一覧のトークン表示を追加（Step 5-2）
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.controllers.api_setting_controller import _seed_default_free_models_if_empty
from app.core.config import (
    EXCLUDED_CATEGORIES,
    NEWS_CATEGORIES,
    get_settings,
)
from app.core.config import (
    format_output as get_safe_config_summary,
)
from app.core.database import get_db
from app.repositories.rss_source_repo import RssSourceRepository
from app.repositories.run_repo import RunRepository
from app.repositories.setting_repo import SettingRepository
from app.schemas.article_schema import ONBOARDING_NOTICES
from app.schemas.run_schema import RUN_STATUS_LABELS, RUN_TYPE_LABELS
from app.services.digest_orchestrator import build_view_data_for_run
from app.services.metrics_service import get_phase2_report
from app.services.rss_service import JST_ZONE, format_datetime_jst
from app.services.usage_dashboard_service import (
    build_run_usage_map,
    build_usage_dashboard,
)

# =============================================================================
# 設定値ブロック（テンプレートディレクトリパス）
# =============================================================================

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "views" / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["views"])


def _common_context(db: Session) -> dict[str, Any]:
    """全画面共通のコンテキスト辞書（今日の日付、カテゴリ定義、安全な設定情報）を生成する。"""
    now_jst = datetime.now(UTC).astimezone(JST_ZONE)
    setting = SettingRepository(db).get_or_create()
    models = _seed_default_free_models_if_empty(db)
    return {
        "today_jst_str": now_jst.strftime("%Y/%m/%d"),
        "today_iso": now_jst.date().isoformat(),
        "news_categories": NEWS_CATEGORIES,
        "excluded_categories_map": EXCLUDED_CATEGORIES,
        "user_setting": setting,
        "free_models": models,
        "safe_env": get_safe_config_summary(get_settings()),
        "onboarding_notices": list(ONBOARDING_NOTICES),
    }


@router.get("/", response_class=HTMLResponse)
def view_index(
    request: Request,
    run_id: str | None = Query(default=None),
    display_count: int | None = Query(default=None, ge=1, le=15),
    sort_order: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """基本画面を表示する（初回未承認の場合は初回確認画面を表示：指示書 §6, §17）。"""
    ctx = _common_context(db)
    setting = ctx["user_setting"]

    if not setting.is_initial_approved and run_id is None:
        return templates.TemplateResponse(
            request=request,
            name="onboarding.html",
            context=ctx,
        )

    view_data = build_view_data_for_run(
        session=db,
        run_id=run_id,
        display_count_override=display_count,
        sort_order_override=sort_order,
    )
    ctx.update(
        {
            "cards": view_data["cards"],
            "summary": view_data["summary"],
            "current_run": view_data["run"],
            "last_updated_jst_str": format_datetime_jst(setting.last_successful_run_at),
            "active_display_count": display_count or setting.display_count,
            "active_sort_order": sort_order or setting.sort_order,
        }
    )
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context=ctx,
    )


@router.get("/onboarding", response_class=HTMLResponse)
def view_onboarding(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """初回起動確認画面を表示する（指示書 §6）。"""
    ctx = _common_context(db)
    return templates.TemplateResponse(
        request=request,
        name="onboarding.html",
        context=ctx,
    )


@router.get("/history", response_class=HTMLResponse)
def view_history(
    request: Request,
    run_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """過去のニュース処理実行履歴画面を表示する（指示書 §18）。"""
    ctx = _common_context(db)
    run_repo = RunRepository(db)
    runs = run_repo.list_runs()

    selected_run_id = run_id or (runs[0].run_id if runs else None)
    view_data = build_view_data_for_run(session=db, run_id=selected_run_id)
    ctx.update(
        {
            "runs": runs,
            "run_usage_map": build_run_usage_map(db, runs),
            "selected_run_id": selected_run_id,
            "cards": view_data["cards"],
            "summary": view_data["summary"],
            "format_datetime_jst": format_datetime_jst,
            "run_type_labels": RUN_TYPE_LABELS,
            "run_status_labels": RUN_STATUS_LABELS,
        }
    )
    return templates.TemplateResponse(
        request=request,
        name="history.html",
        context=ctx,
    )


@router.get("/settings", response_class=HTMLResponse)
def view_settings(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """設定画面を表示する（指示書 §17.6）。"""
    ctx = _common_context(db)
    rss_sources = RssSourceRepository(db).seed_initial_sources()
    runs = RunRepository(db).list_runs(limit=20)
    ctx.update(
        {
            "rss_sources": rss_sources,
            "recent_runs": runs,
            "format_datetime_jst": format_datetime_jst,
        }
    )
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context=ctx,
    )


@router.get("/metrics", response_class=HTMLResponse)
def view_metrics(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Phase 2 のRSS取得量計測レポート画面を表示する。"""
    ctx = _common_context(db)
    ctx.update({"report": get_phase2_report(db)})
    return templates.TemplateResponse(
        request=request,
        name="metrics.html",
        context=ctx,
    )


@router.get("/usage", response_class=HTMLResponse)
def view_usage(
    request: Request,
    run_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """AI使用量・無料モデル運用ダッシュボードを表示する（指示書 §14〜§18）。"""
    ctx = _common_context(db)
    ctx.update(
        {
            "dashboard": build_usage_dashboard(session=db, run_id=run_id),
            "format_datetime_jst": format_datetime_jst,
            "run_type_labels": RUN_TYPE_LABELS,
            "run_status_labels": RUN_STATUS_LABELS,
        }
    )
    return templates.TemplateResponse(
        request=request,
        name="usage.html",
        context=ctx,
    )


def validate_input(data: dict[str, Any]) -> dict[str, Any]:
    """入力辞書を検証する。"""
    return dict(data)


def execute_logic(validated: dict[str, Any]) -> dict[str, Any]:
    """テンプレートディレクトリおよびルート数を返す。"""
    return {
        "templates_dir_exists": TEMPLATES_DIR.exists(),
        "routes_count": len(router.routes),
    }


def format_output(result: dict[str, Any]) -> dict[str, Any]:
    """結果辞書を整形して返す。"""
    return dict(result)


def main(data: dict[str, Any] | None = None) -> dict[str, Any]:
    """View Controllerの構成情報を返す。"""
    validated = validate_input(data or {})
    res = execute_logic(validated)
    return format_output(res)
