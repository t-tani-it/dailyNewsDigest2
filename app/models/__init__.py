"""全SQLAlchemy ORMモデルの集約パッケージ。

目的（Why）:
    Alembicマイグレーションおよび `Base.metadata.create_all()` が
    全10テーブルの定義を確実に認識できるよう1箇所でインポート・公開する。
仕様（What）:
    10テーブルに対応するORMクラスの公開と `init_all_tables` ヘルパーの提供。
前提条件・依存関係（Assumption / Dependencies）:
    `app.core.database`, `app.models.*`
入出力（I/O）:
    入力: SQLAlchemy `Engine`
    出力: 登録済みテーブル名一覧
注意点（Caution）:
    新規テーブルモデルを追加した場合は必ず本ファイルの `__all__` にも追加すること。
今後の拡張ポイント（Future Work）:
    将来フェーズ用のブックマーク・RAGインデックスモデル追加。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 1-4）
"""

from __future__ import annotations

from sqlalchemy import Engine

from app.core.database import Base, get_engine
from app.models.analysis import NewsAnalysis
from app.models.article import NewsArticle
from app.models.metrics import DailyRssMetric
from app.models.model_catalog import ModelCatalog, PriceSnapshot
from app.models.rss_source import RssSource
from app.models.run import NewsRun, RunArticle
from app.models.setting import UserSetting
from app.models.usage_log import LlmUsageLog

# =============================================================================
# 設定値ブロック（管理対象の全10テーブル名一覧）
# =============================================================================

EXPECTED_TABLE_NAMES: tuple[str, ...] = (
    "rss_sources",
    "news_articles",
    "news_runs",
    "news_analyses",
    "run_articles",
    "llm_usage_logs",
    "model_catalog",
    "price_snapshots",
    "user_settings",
    "daily_rss_metrics",
)

__all__: list[str] = [
    "EXPECTED_TABLE_NAMES",
    "Base",
    "DailyRssMetric",
    "LlmUsageLog",
    "ModelCatalog",
    "NewsAnalysis",
    "NewsArticle",
    "NewsRun",
    "PriceSnapshot",
    "RssSource",
    "RunArticle",
    "UserSetting",
    "init_all_tables",
]


def init_all_tables(engine: Engine | None = None) -> list[str]:
    """指定されたエンジン上に全10テーブルを作成する（既存テーブルは維持）。

    Args:
        engine (Engine | None): 対象エンジン。省略時はデフォルトエンジンを使用。

    Returns:
        list[str]: メタデータに登録されているテーブル名一覧。

    Side Effects:
        データベースに対して `CREATE TABLE IF NOT EXISTS` を発行する。

    Examples:
        >>> from app.core.database import create_db_engine
        >>> eng = create_db_engine("sqlite:///:memory:")
        >>> len(init_all_tables(eng))
        10
    """
    target_engine = engine or get_engine()
    Base.metadata.create_all(bind=target_engine)
    return list(Base.metadata.tables.keys())
