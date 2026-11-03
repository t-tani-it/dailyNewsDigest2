"""日次RSS取得量計測（daily_rss_metrics）のRepositoryモジュール。

目的（Why）:
    Phase 2 で必要となる「日別のRSS取得件数・重複率・失敗率・文字数・推定トークン量」を
    PostgreSQL / SQLite へ永続化し、2日分の実測が揃ったかどうかを判定できるようにする
    （指示書 §22 Phase 2、期間は2日へ読み替え）。
仕様（What）:
    `MetricsRepository` クラスによる日次計測データの登録・上書き・一覧取得・実測日数集計。
前提条件・依存関係（Assumption / Dependencies）:
    sqlalchemy>=2.0, `app.models.metrics`
入出力（I/O）:
    入力: SQLAlchemy `Session`、計測データ辞書
    出力: `DailyRssMetric` インスタンスまたはそのリスト
注意点（Caution）:
    同一日付の計測は「加算」ではなく「最新スナップショットで上書き」する。
    RSS取得量は日中の取得回数に比例して増える合計値ではないためである。
今後の拡張ポイント（Future Work）:
    計測期間の開始日・終了日を指定した範囲集計メソッドの追加。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 3-1）
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.metrics import DailyRssMetric
from app.models.rss_source import utc_now

# =============================================================================
# 設定値ブロック
# =============================================================================

# 一覧取得時の既定最大件数（約2か月分の日次データ）
DEFAULT_METRICS_LIMIT: int = 60

# upsert で更新を許可するカラム（metric_date は主キーのため変更しない）
UPDATABLE_FIELDS: tuple[str, ...] = (
    "is_actual_measurement",
    "total_fetched",
    "deduped_count",
    "duplicate_rate",
    "category_counts",
    "rss_failure_rate",
    "avg_title_chars",
    "avg_summary_chars",
    "estimated_input_tokens",
)


class MetricsRepository:
    """`daily_rss_metrics` テーブルへの永続化を担当するRepositoryクラス。"""

    def __init__(self, session: Session) -> None:
        """Repositoryを初期化する。

        Args:
            session (Session): SQLAlchemy セッション。
        """
        self.session = session

    def get_by_date(self, metric_date: date) -> DailyRssMetric | None:
        """指定日付の計測レコードを取得する。

        Args:
            metric_date (date): 計測日（日本時間）。

        Returns:
            DailyRssMetric | None: 存在する場合はモデル、無ければ None。

        Side Effects:
            なし。
        """
        return self.session.get(DailyRssMetric, metric_date)

    def upsert_daily_metric(self, metric_data: dict[str, Any]) -> DailyRssMetric:
        """日次計測データを登録、既存日付なら最新値で上書きする。

        Args:
            metric_data (dict[str, Any]): `metric_date` を必須とし、
                `total_fetched`, `deduped_count` 等を含む辞書。

        Returns:
            DailyRssMetric: 保存後の計測モデル。

        Raises:
            ValueError: `metric_date` が未指定の場合。

        Side Effects:
            `daily_rss_metrics` へ INSERT または UPDATE し commit する。

        Examples:
            >>> from datetime import date
            >>> repo.upsert_daily_metric({"metric_date": date(2026, 11, 2), "total_fetched": 10})
            <DailyRssMetric ...>
        """
        metric_date = metric_data.get("metric_date")
        if not isinstance(metric_date, date):
            raise ValueError("metric_data には date 型の metric_date が必須です。")

        existing = self.get_by_date(metric_date)
        if existing is None:
            existing = DailyRssMetric(metric_date=metric_date)
            self.session.add(existing)

        for field in UPDATABLE_FIELDS:
            if field in metric_data:
                setattr(existing, field, metric_data[field])

        existing.recorded_at = utc_now()
        self.session.commit()
        self.session.refresh(existing)
        return existing

    def list_metrics(
        self,
        limit: int = DEFAULT_METRICS_LIMIT,
        ascending: bool = True,
    ) -> list[DailyRssMetric]:
        """日次計測データを日付順で取得する。

        Args:
            limit (int): 最大取得件数。
            ascending (bool): True なら古い順、False なら新しい順。

        Returns:
            list[DailyRssMetric]: 計測レコードのリスト。

        Side Effects:
            なし。
        """
        order_column = (
            DailyRssMetric.metric_date.asc()
            if ascending
            else DailyRssMetric.metric_date.desc()
        )
        stmt = select(DailyRssMetric).order_by(order_column).limit(int(limit))
        return list(self.session.scalars(stmt).all())

    def count_actual_measurement_days(self) -> int:
        """実測フラグが立っている日次レコードの件数（＝実測日数）を返す。

        Returns:
            int: 実測日数。

        Side Effects:
            なし。
        """
        stmt = select(func.count(DailyRssMetric.metric_date)).where(
            DailyRssMetric.is_actual_measurement.is_(True)
        )
        return int(self.session.scalar(stmt) or 0)

    def get_latest_metric_date(self) -> date | None:
        """最新の計測日を返す。

        Returns:
            date | None: 最新計測日。レコードが無い場合は None。

        Side Effects:
            なし。
        """
        stmt = select(func.max(DailyRssMetric.metric_date))
        return self.session.scalar(stmt)


def validate_input(data: dict[str, Any]) -> dict[str, Any]:
    """計測Repository操作用の入力辞書を検証する。

    Args:
        data (dict[str, Any]): `session` と `metric_data` を含む辞書。

    Returns:
        dict[str, Any]: 検証済み辞書。

    Raises:
        ValueError: セッションまたは計測データが不正な場合。

    Side Effects:
        なし。
    """
    if "session" not in data or not isinstance(data["session"], Session):
        raise ValueError("有効な SQLAlchemy Session が必要です。")
    if "metric_data" in data and not isinstance(data["metric_data"], dict):
        raise ValueError("metric_data は dict である必要があります。")
    return dict(data)


def execute_logic(validated: dict[str, Any]) -> Any:
    """検証済み入力に応じて登録または一覧取得を実行する。"""
    repo = MetricsRepository(validated["session"])
    action = validated.get("action", "list")
    if action == "upsert":
        return repo.upsert_daily_metric(validated["metric_data"])
    if action == "count_actual":
        return repo.count_actual_measurement_days()
    return repo.list_metrics()


def format_output(result: Any) -> Any:
    """Repository結果をそのまま返す（整形はService層で行う）。"""
    return result


def main(data: dict[str, Any]) -> Any:
    """計測Repositoryの一連の処理を実行する。"""
    validated = validate_input(data)
    res = execute_logic(validated)
    return format_output(res)
