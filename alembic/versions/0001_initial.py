"""初期データベーススキーマ（全10テーブル）の作成マイグレーション。

目的（Why）:
    dailyNewsDigest2 が必要とする全10テーブル・一意制約・外部キー・インデックスを
    PostgreSQL / SQLite 上に一括作成する（指示書 §19）。
仕様（What）:
    `rss_sources`, `news_articles`, `news_runs`, `news_analyses`, `run_articles`,
    `llm_usage_logs`, `model_catalog`, `price_snapshots`, `user_settings`, `daily_rss_metrics`
    の作成と破棄。
前提条件・依存関係（Assumption / Dependencies）:
    alembic>=1.13, sqlalchemy>=2.0
入出力（I/O）:
    DDL発行によるテーブル作成（upgrade）および削除（downgrade）。
注意点（Caution）:
    日時カラムはすべて `DateTime(timezone=True)` で統一する。
今後の拡張ポイント（Future Work）:
    Phase 4 以降の追加インデックスマイグレーション。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 1-4）
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# =============================================================================
# 設定値ブロック（Alembic リビジョン識別子）
# =============================================================================

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """全10テーブル・制約・インデックスを作成する。

    Side Effects:
        対象データベースに10個のテーブルと関連インデックスを作成する。
    """
    op.create_table(
        "rss_sources",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("url", sa.String(length=500), nullable=False),
        sa.Column("category_hint", sa.String(length=50), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("terms_status", sa.String(length=30), nullable=False),
        sa.Column("allow_store_summary", sa.Boolean(), nullable=False),
        sa.Column("allow_ai_summary", sa.Boolean(), nullable=False),
        sa.Column("last_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.String(length=50), nullable=False),
        sa.Column("error_history", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("url"),
    )
    op.create_index("ix_rss_sources_url", "rss_sources", ["url"], unique=True)

    op.create_table(
        "news_articles",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=True),
        sa.Column("source_name", sa.String(length=150), nullable=False),
        sa.Column("guid", sa.String(length=500), nullable=True),
        sa.Column("url", sa.String(length=1000), nullable=False),
        sa.Column("normalized_url", sa.String(length=1000), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("title_hash", sa.String(length=64), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at_rss", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_pubdate_verified", sa.Boolean(), nullable=False),
        sa.Column("rss_categories", sa.JSON(), nullable=False),
        sa.Column("summary_snippet", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("cluster_id", sa.String(length=64), nullable=True),
        sa.Column("is_updated_article", sa.Boolean(), nullable=False),
        sa.Column("is_read", sa.Boolean(), nullable=False),
        sa.Column("is_excluded", sa.Boolean(), nullable=False),
        sa.Column("exclusion_reason", sa.String(length=100), nullable=True),
        sa.Column("primary_category", sa.String(length=50), nullable=False),
        sa.Column("importance_score", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["rss_sources.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("normalized_url"),
    )
    op.create_index("ix_news_articles_source_id", "news_articles", ["source_id"])
    op.create_index("ix_news_articles_guid", "news_articles", ["guid"])
    op.create_index(
        "ix_news_articles_normalized_url", "news_articles", ["normalized_url"], unique=True
    )
    op.create_index("ix_news_articles_title_hash", "news_articles", ["title_hash"])
    op.create_index("ix_news_articles_published_at", "news_articles", ["published_at"])
    op.create_index("ix_news_articles_cluster_id", "news_articles", ["cluster_id"])
    op.create_index("ix_news_articles_is_excluded", "news_articles", ["is_excluded"])
    op.create_index("ix_news_articles_primary_category", "news_articles", ["primary_category"])

    op.create_table(
        "news_runs",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("parent_run_id", sa.String(length=36), nullable=True),
        sa.Column("run_type", sa.String(length=30), nullable=False),
        sa.Column("target_date", sa.Date(), nullable=True),
        sa.Column("selected_categories", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetched_count", sa.Integer(), nullable=False),
        sa.Column("deduped_count", sa.Integer(), nullable=False),
        sa.Column("ai_classified_count", sa.Integer(), nullable=False),
        sa.Column("ai_summarized_count", sa.Integer(), nullable=False),
        sa.Column("displayed_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("reused_analysis", sa.Boolean(), nullable=False),
        sa.Column("used_provider", sa.String(length=50), nullable=True),
        sa.Column("used_model_id", sa.String(length=150), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["parent_run_id"], ["news_runs.run_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index("ix_news_runs_parent_run_id", "news_runs", ["parent_run_id"])
    op.create_index("ix_news_runs_started_at", "news_runs", ["started_at"])

    op.create_table(
        "news_analyses",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("article_id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=True),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("model_id", sa.String(length=150), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("is_excluded", sa.Boolean(), nullable=False),
        sa.Column("importance_score", sa.Integer(), nullable=False),
        sa.Column("summary_ja", sa.Text(), nullable=False),
        sa.Column("why_important", sa.Text(), nullable=False),
        sa.Column("summary_status", sa.String(length=30), nullable=False),
        sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["article_id"], ["news_articles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["news_runs.run_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_news_analyses_article_id", "news_analyses", ["article_id"])
    op.create_index("ix_news_analyses_run_id", "news_analyses", ["run_id"])

    op.create_table(
        "run_articles",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("article_id", sa.Integer(), nullable=False),
        sa.Column("analysis_id", sa.Integer(), nullable=True),
        sa.Column("display_rank", sa.Integer(), nullable=False),
        sa.Column("snapshot_category", sa.String(length=50), nullable=False),
        sa.Column("snapshot_importance", sa.Integer(), nullable=False),
        sa.Column("snapshot_summary", sa.Text(), nullable=True),
        sa.Column("snapshot_why_important", sa.Text(), nullable=True),
        sa.Column("is_reused", sa.Boolean(), nullable=False),
        sa.Column("selected_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["analysis_id"], ["news_analyses.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["article_id"], ["news_articles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["news_runs.run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "article_id", name="uq_run_article"),
    )
    op.create_index("ix_run_articles_run_id", "run_articles", ["run_id"])
    op.create_index("ix_run_articles_article_id", "run_articles", ["article_id"])

    op.create_table(
        "llm_usage_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("call_id", sa.String(length=36), nullable=False),
        sa.Column("purpose", sa.String(length=30), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("model_id", sa.String(length=150), nullable=False),
        sa.Column("called_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("reasoning_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("reasoning_included_in_output", sa.Boolean(), nullable=False),
        sa.Column("usage_status", sa.String(length=30), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("switched_from_model", sa.String(length=150), nullable=True),
        sa.Column("switch_reason", sa.String(length=200), nullable=True),
        sa.Column("target_article_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["news_runs.run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("call_id"),
    )
    op.create_index("ix_llm_usage_logs_run_id", "llm_usage_logs", ["run_id"])
    op.create_index("ix_llm_usage_logs_provider", "llm_usage_logs", ["provider"])
    op.create_index("ix_llm_usage_logs_model_id", "llm_usage_logs", ["model_id"])
    op.create_index("ix_llm_usage_logs_called_at", "llm_usage_logs", ["called_at"])

    op.create_table(
        "model_catalog",
        sa.Column("model_id", sa.String(length=150), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("model_name", sa.String(length=200), nullable=False),
        sa.Column("free_tier_status", sa.String(length=30), nullable=False),
        sa.Column("input_price_per_1m", sa.Float(), nullable=True),
        sa.Column("output_price_per_1m", sa.Float(), nullable=True),
        sa.Column("request_price", sa.Float(), nullable=True),
        sa.Column("context_length", sa.Integer(), nullable=True),
        sa.Column("modalities", sa.JSON(), nullable=False),
        sa.Column("supports_structured_output", sa.Boolean(), nullable=False),
        sa.Column("availability_status", sa.String(length=30), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_info", sa.String(length=255), nullable=False),
        sa.PrimaryKeyConstraint("model_id"),
    )
    op.create_index("ix_model_catalog_provider", "model_catalog", ["provider"])

    op.create_table(
        "price_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("model_id", sa.String(length=150), nullable=False),
        sa.Column("model_name", sa.String(length=200), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("input_price_per_1m", sa.Float(), nullable=True),
        sa.Column("output_price_per_1m", sa.Float(), nullable=True),
        sa.Column("price_tier", sa.String(length=50), nullable=False),
        sa.Column("is_verified", sa.Boolean(), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_url", sa.String(length=500), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("model_id"),
    )
    op.create_index("ix_price_snapshots_model_id", "price_snapshots", ["model_id"], unique=True)

    op.create_table(
        "user_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("is_initial_approved", sa.Boolean(), nullable=False),
        sa.Column("selected_categories", sa.JSON(), nullable=False),
        sa.Column("excluded_categories", sa.JSON(), nullable=False),
        sa.Column("display_count", sa.Integer(), nullable=False),
        sa.Column("sort_order", sa.String(length=30), nullable=False),
        sa.Column("target_days", sa.Integer(), nullable=False),
        sa.Column("auto_update_enabled", sa.Boolean(), nullable=False),
        sa.Column("auto_update_time", sa.String(length=10), nullable=False),
        sa.Column("summary_char_limit", sa.Integer(), nullable=False),
        sa.Column("llm_enabled", sa.Boolean(), nullable=False),
        sa.Column("selection_mode", sa.String(length=30), nullable=False),
        sa.Column("preferred_model_id", sa.String(length=150), nullable=True),
        sa.Column("auto_switch_enabled", sa.Boolean(), nullable=False),
        sa.Column("daily_api_call_limit", sa.Integer(), nullable=False),
        sa.Column("last_successful_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active_run_lock_id", sa.String(length=36), nullable=True),
        sa.Column("active_run_locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "daily_rss_metrics",
        sa.Column("metric_date", sa.Date(), nullable=False),
        sa.Column("is_actual_measurement", sa.Boolean(), nullable=False),
        sa.Column("total_fetched", sa.Integer(), nullable=False),
        sa.Column("deduped_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_rate", sa.Float(), nullable=False),
        sa.Column("category_counts", sa.JSON(), nullable=False),
        sa.Column("rss_failure_rate", sa.Float(), nullable=False),
        sa.Column("avg_title_chars", sa.Float(), nullable=False),
        sa.Column("avg_summary_chars", sa.Float(), nullable=False),
        sa.Column("estimated_input_tokens", sa.Integer(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("metric_date"),
    )


def downgrade() -> None:
    """全10テーブルを作成逆順で破棄する。

    Side Effects:
        対象データベースから10個のテーブルを削除する。
    """
    op.drop_table("daily_rss_metrics")
    op.drop_table("user_settings")
    op.drop_index("ix_price_snapshots_model_id", table_name="price_snapshots")
    op.drop_table("price_snapshots")
    op.drop_index("ix_model_catalog_provider", table_name="model_catalog")
    op.drop_table("model_catalog")
    op.drop_index("ix_llm_usage_logs_called_at", table_name="llm_usage_logs")
    op.drop_index("ix_llm_usage_logs_model_id", table_name="llm_usage_logs")
    op.drop_index("ix_llm_usage_logs_provider", table_name="llm_usage_logs")
    op.drop_index("ix_llm_usage_logs_run_id", table_name="llm_usage_logs")
    op.drop_table("llm_usage_logs")
    op.drop_index("ix_run_articles_article_id", table_name="run_articles")
    op.drop_index("ix_run_articles_run_id", table_name="run_articles")
    op.drop_table("run_articles")
    op.drop_index("ix_news_analyses_run_id", table_name="news_analyses")
    op.drop_index("ix_news_analyses_article_id", table_name="news_analyses")
    op.drop_table("news_analyses")
    op.drop_index("ix_news_runs_started_at", table_name="news_runs")
    op.drop_index("ix_news_runs_parent_run_id", table_name="news_runs")
    op.drop_table("news_runs")
    op.drop_index("ix_news_articles_primary_category", table_name="news_articles")
    op.drop_index("ix_news_articles_is_excluded", table_name="news_articles")
    op.drop_index("ix_news_articles_cluster_id", table_name="news_articles")
    op.drop_index("ix_news_articles_published_at", table_name="news_articles")
    op.drop_index("ix_news_articles_title_hash", table_name="news_articles")
    op.drop_index("ix_news_articles_normalized_url", table_name="news_articles")
    op.drop_index("ix_news_articles_guid", table_name="news_articles")
    op.drop_index("ix_news_articles_source_id", table_name="news_articles")
    op.drop_table("news_articles")
    op.drop_index("ix_rss_sources_url", table_name="rss_sources")
    op.drop_table("rss_sources")
