"""dailyNewsDigest2 FastAPI アプリケーションエントリポイント。

目的（Why）:
    MVC（Model / View / Controller）+ Service + Repository 構成のWebアプリケーションを
    起動し、静的ファイル配信、ヘルスチェック、および各Controllerルーターを統合する（指示書 §4）。
仕様（What）:
    - `create_app`: FastAPI インスタンス生成・ルーターおよび静的ファイルマウント
    - `GET /health`: Docker ヘルスチェックおよびDB疎通確認エンドポイント
前提条件・依存関係（Assumption / Dependencies）:
    fastapi>=0.115, `app.controllers`, `app.core`, `app.models`
入出力（I/O）:
    HTTPリクエストを受け付け、HTMLまたはJSONレスポンスを返す。
注意点（Caution）:
    `main.py` にはRSS解析やLLM呼び出し処理を直接書かず、各Controllerへ委譲すること（指示書 §4.1）。
今後の拡張ポイント（Future Work）:
    Phase 2 / Phase 4 の計測・使用量ダッシュボードルーター追加。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 2-3 / Step 2-4）
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.controllers import (
    api_metrics_controller,
    api_news_controller,
    api_setting_controller,
    api_usage_controller,
    view_controller,
)
from app.core.config import ALLOW_PAID_LLM_EXECUTION, get_settings
from app.core.database import check_db_connection, get_engine
from app.core.logging import get_logger, setup_logging
from app.models import init_all_tables

# =============================================================================
# 設定値ブロック
# =============================================================================

STATIC_DIR = Path(__file__).resolve().parent / "views" / "static"
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app_instance: FastAPI) -> AsyncGenerator[None, None]:
    """アプリ起動・終了時のライフサイクル管理。

    解説：
        起動時にロガーを初期化し、DB接続が可能な場合はテーブル存在確認を行う。
        DBが未起動でもプロセス自体がクラッシュしないよう例外を捕捉して警告ログに出力する。
    """
    setup_logging()
    try:
        engine = get_engine()
        init_all_tables(engine)
    except Exception as exc:
        logger.warning("起動時のDBテーブル初期化をスキップしました: %s", exc)
    yield


def create_app() -> FastAPI:
    """FastAPI アプリケーションインスタンスを構築して返す。

    Returns:
        FastAPI: ルーターおよび静的ファイル設定済みのアプリケーション。

    Side Effects:
        なし。

    Examples:
        >>> application = create_app()
        >>> application.title
        'dailyNewsDigest2'
    """
    settings = get_settings()
    application = FastAPI(
        title=settings.app_name,
        version="3.0.0",
        description="毎朝国内外のRSSから重要ニュースを最大15件選定・要約する個人向けシステム（無料LLM限定）",
        lifespan=lifespan,
    )

    if STATIC_DIR.exists():
        application.mount(
            "/static",
            StaticFiles(directory=str(STATIC_DIR)),
            name="static",
        )

    application.include_router(view_controller.router)
    application.include_router(api_news_controller.router)
    application.include_router(api_setting_controller.router)
    application.include_router(api_metrics_controller.router)
    application.include_router(api_usage_controller.router)

    @application.get("/health", tags=["system"])
    def health_check() -> dict[str, Any]:
        """ヘルスチェックおよび無料LLMポリシー状態を返すエンドポイント。"""
        db_status = check_db_connection()
        return {
            "status": "ok",
            "app_name": settings.app_name,
            "db_connected": db_status["ok"],
            "allow_paid_llm_execution": ALLOW_PAID_LLM_EXECUTION,
        }

    return application


app = create_app()


def validate_input(data: dict[str, Any] | None = None) -> dict[str, Any]:
    """入力辞書を検証する。"""
    return dict(data or {})


def execute_logic(validated: dict[str, Any]) -> dict[str, Any]:
    """アプリ構成サマリーを取得する。"""
    return {
        "title": app.title,
        "routes_count": len(app.routes),
        "allow_paid_llm_execution": ALLOW_PAID_LLM_EXECUTION,
    }


def format_output(result: dict[str, Any]) -> dict[str, Any]:
    """結果辞書を整形して返す。"""
    return dict(result)


def main(data: dict[str, Any] | None = None) -> dict[str, Any]:
    """アプリ構成確認を実行する。"""
    validated = validate_input(data)
    res = execute_logic(validated)
    return format_output(res)
