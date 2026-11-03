"""SQLAlchemy データベース接続・セッション管理モジュール。

目的（Why）:
    PostgreSQL（本番・Docker Compose）および SQLite（自動テスト用）への接続エンジンと
    トランザクションセッションを一元管理し、接続リークや未コミット状態を防ぐ。
仕様（What）:
    - SQLAlchemy 2.x `DeclarativeBase` を継承した `Base` クラスの定義
    - `create_db_engine`: 接続URLに応じた Engine 生成（SQLite時は `check_same_thread=False`）
    - `get_db`: FastAPI の依存性注入（Depends）用セッションジェネレータ
    - `session_scope`: バッチ・スケジューラ用のトランザクションコンテキストマネージャ
    - `check_db_connection`: DB疎通確認関数
前提条件・依存関係（Assumption / Dependencies）:
    sqlalchemy>=2.0, psycopg>=3.1, `app.core.config`, `app.core.logging`
入出力（I/O）:
    入力: データベース接続URL文字列
    出力: SQLAlchemy `Engine`, `Session` インスタンス、および接続診断結果辞書
注意点（Caution）:
    接続エラーをログ出力する際は、DBパスワードが露出しないよう必ず `logging` モジュール経由で出力すること。
今後の拡張ポイント（Future Work）:
    リードレプリカ接続や非同期エンジン（AsyncEngine）への対応。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 1-3）
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import get_settings
from app.core.logging import get_logger, mask_sensitive_text

# =============================================================================
# 設定値ブロック（DBコネクションプールの既定パラメータ）
# =============================================================================

DEFAULT_POOL_PRE_PING: bool = True
SUPPORTED_DB_SCHEMES: tuple[str, ...] = (
    "postgresql://",
    "postgresql+psycopg://",
    "sqlite://",
)

logger = get_logger(__name__)


class Base(DeclarativeBase):
    """全SQLAlchemy ORMモデルが継承する基底クラス。"""


# モジュール内で保持するデフォルトEngineおよびSessionファクトリ
_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def validate_input(database_url: str | None = None) -> str:
    """データベース接続URLの形式を検証する。

    Args:
        database_url (str | None): 接続URL。None の場合は `get_settings().database_url` を使用する。

    Returns:
        str: 検証済みの接続URL文字列。

    Raises:
        ValueError: 空文字または未対応スキームが指定された場合。

    Side Effects:
        なし。

    Examples:
        >>> validate_input("sqlite:///:memory:")
        'sqlite:///:memory:'
    """
    target_url = (database_url if database_url is not None else get_settings().database_url).strip()
    if not target_url:
        raise ValueError("database_url が空です。")
    if not target_url.lower().startswith(SUPPORTED_DB_SCHEMES):
        raise ValueError("未対応のデータベース接続スキームです。")
    return target_url


def create_db_engine(database_url: str | None = None) -> Engine:
    """接続URLに応じた SQLAlchemy `Engine` を生成する。

    解説：
        通常運用では PostgreSQL（`postgresql+psycopg://`）へ接続し、
        単体テスト実行時には外部DB無しで高速検証できるよう SQLite（`sqlite://`）にも対応する。

    Args:
        database_url (str | None): 接続先URL。省略時は設定値を使用。

    Returns:
        Engine: SQLAlchemy エンジンインスタンス。

    Raises:
        ValueError: 接続URLが不正な場合。

    Side Effects:
        エンジンオブジェクトを初期化する（この時点では実TCP接続は行われない）。

    Examples:
        >>> eng = create_db_engine("sqlite:///:memory:")
        >>> eng.dialect.name
        'sqlite'
    """
    valid_url = validate_input(database_url)
    if valid_url.lower().startswith("sqlite://"):
        connect_args: dict[str, Any] = {"check_same_thread": False}
        if ":memory:" in valid_url:
            return create_engine(
                valid_url,
                connect_args=connect_args,
                poolclass=StaticPool,
                future=True,
            )
        return create_engine(valid_url, connect_args=connect_args, future=True)

    return create_engine(
        valid_url,
        pool_pre_ping=DEFAULT_POOL_PRE_PING,
        future=True,
    )


def get_engine(database_url: str | None = None, force_recreate: bool = False) -> Engine:
    """アプリケーション共通の `Engine` を取得（未生成時は生成）する。

    Args:
        database_url (str | None): 明示的に接続先を指定する場合のURL。
        force_recreate (bool): True の場合、既存エンジンを破棄して再作成する（テスト切替用）。

    Returns:
        Engine: SQLAlchemy エンジン。

    Side Effects:
        モジュール内の `_engine` と `_session_factory` を更新する。

    Examples:
        >>> eng = get_engine("sqlite:///:memory:", force_recreate=True)
        >>> eng.dialect.name
        'sqlite'
    """
    global _engine, _session_factory
    if _engine is None or force_recreate or database_url is not None:
        if _engine is not None and force_recreate:
            _engine.dispose()
        _engine = create_db_engine(database_url)
        _session_factory = sessionmaker(
            bind=_engine,
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
            class_=Session,
        )
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """SQLAlchemy の `sessionmaker` インスタンスを取得する。

    Returns:
        sessionmaker[Session]: セッション生成ファクトリ。

    Side Effects:
        エンジン未初期化の場合は初期化する。

    Examples:
        >>> _ = get_engine("sqlite:///:memory:", force_recreate=True)
        >>> factory = get_session_factory()
        >>> callable(factory)
        True
    """
    if _session_factory is None:
        get_engine()
    assert _session_factory is not None
    return _session_factory


def get_db() -> Generator[Session, None, None]:
    """FastAPI の `Depends(get_db)` で利用するリクエスト単位のDBセッションを提供する。

    Yields:
        Session: SQLAlchemy セッション。リクエスト終了時に確実にクローズされる。

    Side Effects:
        DBセッションのオープンおよびクローズを行う。

    Examples:
        >>> _ = get_engine("sqlite:///:memory:", force_recreate=True)
        >>> gen = get_db()
        >>> db = next(gen)
        >>> isinstance(db, Session)
        True
        >>> gen.close()
    """
    session = get_session_factory()()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """定期実行ワーカーやサービステストで利用するトランザクションスコープを提供する。

    Yields:
        Session: 正常終了時に commit、例外発生時に rollback されるセッション。

    Side Effects:
        DBへの commit / rollback / close を行う。

    Examples:
        >>> _ = get_engine("sqlite:///:memory:", force_recreate=True)
        >>> with session_scope() as s:
        ...     res = s.execute(text("SELECT 1")).scalar()
        >>> res
        1
    """
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check_db_connection(engine_override: Engine | None = None) -> dict[str, Any]:
    """DBへ `SELECT 1` を発行して疎通確認を行い、安全な診断結果を返す。

    Args:
        engine_override (Engine | None): テスト等で個別指定する Engine。

    Returns:
        dict[str, Any]: `ok` (bool), `dialect` (str), `error` (str | None) を含む辞書。

    Side Effects:
        対象DBへ軽量クエリ `SELECT 1` を1回実行する。

    Examples:
        >>> eng = create_db_engine("sqlite:///:memory:")
        >>> check_db_connection(eng)["ok"]
        True
    """
    target_engine = engine_override or get_engine()
    try:
        with target_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {
            "ok": True,
            "dialect": target_engine.dialect.name,
            "error": None,
        }
    except Exception as exc:
        safe_err = mask_sensitive_text(str(exc))
        logger.warning("データベース接続確認に失敗しました: %s", safe_err)
        return {
            "ok": False,
            "dialect": target_engine.dialect.name,
            "error": safe_err,
        }


def execute_logic(validated_url: str) -> dict[str, Any]:
    """検証済みURLでエンジンを構成し、疎通確認を実行する。

    Args:
        validated_url (str): `validate_input` を通過した接続URL。

    Returns:
        dict[str, Any]: 疎通確認結果辞書。

    Side Effects:
        一時エンジンによる `SELECT 1` クエリ実行。

    Examples:
        >>> execute_logic("sqlite:///:memory:")["ok"]
        True
    """
    temp_engine = create_db_engine(validated_url)
    try:
        return check_db_connection(temp_engine)
    finally:
        temp_engine.dispose()


def format_output(status_dict: dict[str, Any]) -> dict[str, Any]:
    """DB疎通確認結果の辞書を整形して返す。

    Args:
        status_dict (dict[str, Any]): `execute_logic` の出力辞書。

    Returns:
        dict[str, Any]: 整形済みステータス辞書。

    Side Effects:
        なし。

    Examples:
        >>> format_output({"ok": True, "dialect": "sqlite", "error": None})["ok"]
        True
    """
    return {
        "ok": bool(status_dict.get("ok", False)),
        "dialect": str(status_dict.get("dialect", "unknown")),
        "error": status_dict.get("error"),
    }


def main(database_url: str | None = None) -> dict[str, Any]:
    """DB接続URLの検証・疎通確認・結果整形の一連の流れを実行する。

    Args:
        database_url (str | None): 確認対象のDB接続URL。

    Returns:
        dict[str, Any]: 接続確認結果。

    Side Effects:
        指定DBへの疎通確認クエリを実行する。

    Examples:
        >>> main("sqlite:///:memory:")["ok"]
        True
    """
    valid_url = validate_input(database_url)
    raw_status = execute_logic(valid_url)
    return format_output(raw_status)
