"""Alembic マイグレーション実行環境スクリプト。

目的（Why）:
    `app.core.config` の環境変数設定および `app.models` の全10テーブルメタデータを
    Alembic に連携し、オフライン・オンライン双方でマイグレーションを実行可能にする。
仕様（What）:
    `run_migrations_offline` および `run_migrations_online` の定義。
前提条件・依存関係（Assumption / Dependencies）:
    alembic>=1.13, sqlalchemy>=2.0, `app.core.config`, `app.models`
入出力（I/O）:
    対象データベースへのDDL発行。
注意点（Caution）:
    接続URLをハードコードせず、`get_settings().database_url` を優先使用する。
今後の拡張ポイント（Future Work）:
    スキーマ比較時のカスタム型比較フックの追加。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 1-4）
"""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from app.core.config import get_settings
from app.models import Base

# =============================================================================
# 設定値ブロック
# =============================================================================

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 環境変数の DATABASE_URL を Alembic 設定へ反映する
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """DBへ実接続せずにSQLスクリプト出力モードでマイグレーションを実行する。

    Side Effects:
        標準出力または指定先へDDL文を出力する。
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """DBへ実接続してオンラインでマイグレーションを適用する。

    Side Effects:
        対象データベースにマイグレーションDDLを適用する。
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
