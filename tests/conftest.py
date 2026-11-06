"""pytest 共通フィクスチャ（SQLite インメモリDB・FastAPI テストクライアント）。

目的（Why）:
    PostgreSQL 18を起動しなくても Phase 1 / Phase 2 のロジックを高速に検証できるよう、
    テストごとに独立した SQLite インメモリDBと FastAPI `TestClient` を提供する。
仕様（What）:
    - `db_engine`: 全10テーブルを作成した SQLite インメモリ Engine
    - `db_session`: テスト用 SQLAlchemy セッション
    - `client`: 依存性注入をテストDBへ向けた FastAPI テストクライアント
前提条件・依存関係（Assumption / Dependencies）:
    pytest>=8.0, httpx, `app.core.database`, `app.models`, `app.main`
入出力（I/O）:
    各テスト関数へ Engine / Session / TestClient を注入する。
注意点（Caution）:
    本番用 PostgreSQL には一切接続しない。`get_engine(..., force_recreate=True)` で
    テストごとにエンジンを破棄・再生成し、テスト間のデータ混入を防ぐ。
今後の拡張ポイント（Future Work）:
    PostgreSQL コンテナを用いた結合テスト用フィクスチャの追加。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 2-5）
    2026-11-06 - 実 `.env` のLLM設定をテストへ持ち込まないための自動フィクスチャを追加
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import Base, get_engine, get_session_factory
from app.main import app
from app.models import init_all_tables

# =============================================================================
# 設定値ブロック（テスト用DBファイル名）
# =============================================================================

TEST_DATABASE_FILENAME: str = "test_daily_digest.db"


@pytest.fixture(autouse=True)
def isolate_external_llm_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """実 `.env` のLLM設定をテストへ持ち込まず、外部API呼び出しを防止する。

    解説：
        `.env` に `LLM_ENABLED=true` や実APIキーが設定されている場合でも、
        テストが実Gemini / OpenRouterへ通信しないよう、共有設定をテスト中だけ
        安全側へ上書きする。LLM機能を検証するテストは、明示的にモックProviderを
        渡すか、DB設定を個別に更新する。

    Args:
        monkeypatch (pytest.MonkeyPatch): テスト終了時に設定を自動復元するfixture。

    Side Effects:
        `get_settings()` が返す共有設定インスタンスのLLM関連値のみを一時変更する。
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_enabled", False)
    monkeypatch.setattr(settings, "gemini_enabled", False)
    monkeypatch.setattr(settings, "openrouter_enabled", False)
    monkeypatch.setattr(settings, "gemini_free_tier_confirmed", False)
    monkeypatch.setattr(settings, "gemini_api_key", "")
    monkeypatch.setattr(settings, "openrouter_api_key", "")


@pytest.fixture()
def db_engine(tmp_path: Path) -> Generator[Engine, None, None]:
    """テストごとに独立した SQLite ファイルDBの Engine を提供する。

    解説：
        インメモリDB（StaticPool）は全セッションが同一コネクションを共有するため、
        アプリのリクエスト処理とテスト側セッションが同時にトランザクションを開始すると
        競合しやすい。テストごとの一時ファイルDBにすることで、セッション同士を
        独立したコネクションとして扱える。

    Args:
        tmp_path (Path): pytest が用意するテスト専用の一時ディレクトリ。

    Yields:
        Engine: テスト専用の SQLAlchemy エンジン。

    Side Effects:
        テスト終了時に全テーブルを削除し、エンジンを破棄する。
    """
    database_url = f"sqlite:///{(tmp_path / TEST_DATABASE_FILENAME).as_posix()}"
    engine = get_engine(database_url, force_recreate=True)
    init_all_tables(engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture()
def db_session(db_engine: Engine) -> Generator[Session, None, None]:
    """テスト専用DBに接続した SQLAlchemy セッションを提供する。

    Args:
        db_engine (Engine): `db_engine` フィクスチャ。

    Yields:
        Session: テスト用セッション。

    Side Effects:
        テスト終了時にセッションをクローズする。
    """
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(db_engine: Engine) -> Generator[TestClient, None, None]:
    """テストDBを参照する FastAPI `TestClient` を提供する。

    Args:
        db_engine (Engine): テスト用エンジン（`get_db` が同一エンジンを参照する）。

    Yields:
        TestClient: FastAPI アプリのテストクライアント。

    Side Effects:
        アプリの lifespan（テーブル初期化）を実行し、終了時にシャットダウンする。
    """
    with TestClient(app) as test_client:
        yield test_client
