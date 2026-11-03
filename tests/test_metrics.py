"""Phase 2: RSS取得量計測・2日分実測判定・レポート集計のテスト。

目的（Why）:
    LLM分析前にRSSだけで取得量を測るPhase 2の計算・保存・判定が正しく動くことを検証する。
    特に「1日分では実測完了と表示しない」「2日分揃って初めて実測完了になる」
    「計測専用実行では記事をDB保存しない」という安全側の振る舞いを確認する。
仕様（What）:
    - `build_daily_metric_data`: 重複率・カテゴリ別件数・平均文字数・推定トークン
    - `record_daily_metric`: 同一日の上書き
    - `get_phase2_report`: 暫定/実測完了の判定
    - `run_rss_measurement`: RSSのみ実行・記事保存なし
    - `execute_digest_run`: 通常実行から計測値が記録されること
前提条件・依存関係（Assumption / Dependencies）:
    pytest, `app.services.metrics_service`, `app.services.digest_orchestrator`
入出力（I/O）:
    テスト関数ごとに assert で合否を判定する。
注意点（Caution）:
    RSSの外部通信は `monkeypatch` で差し替える。
今後の拡張ポイント（Future Work）:
    PostgreSQL 上でのJSONカテゴリ集計の結合テスト。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 3-1）
"""

from __future__ import annotations

from datetime import date
from urllib.parse import quote

import pytest

from app.models.article import NewsArticle
from app.models.metrics import DailyRssMetric
from app.repositories.metrics_repo import MetricsRepository
from app.services import digest_orchestrator, metrics_service
from app.services.metrics_service import (
    build_daily_metric_data,
    get_phase2_report,
    record_daily_metric,
    run_rss_measurement,
)

# =============================================================================
# 設定値ブロック（テスト用の計測日・記事）
# =============================================================================

METRIC_DATE_DAY1 = date(2026, 11, 1)
METRIC_DATE_DAY2 = date(2026, 11, 2)


def _item(
    *,
    title: str,
    summary: str | None,
    category: str = "ai_llm",
    is_excluded: bool = False,
) -> dict[str, object]:
    """計測計算用の記事辞書を生成する。"""
    article_url = f"https://example.com/news/{quote(title, safe='')}"
    return {
        "source_id": 1,
        "source_name": "テストメディア",
        "guid": title,
        "url": article_url,
        "normalized_url": article_url,
        "title": title,
        "summary_snippet": summary,
        "primary_category": category,
        "is_excluded": is_excluded,
    }


def _fake_collected(items: list[dict[str, object]], failure_rate: float = 0.0) -> dict[str, object]:
    """`collect_rss` 互換の収集結果辞書を生成する。"""
    return {
        "all_fetched_items": items,
        "target_items": items,
        "unverified_pubdate_count": 0,
        "out_of_range_notice": None,
        "source_results": [],
        "success_sources": 1,
        "failed_sources": 0,
        "rss_failure_rate": failure_rate,
    }


# -----------------------------------------------------------------------------
# 日次計測値の計算
# -----------------------------------------------------------------------------


def test_build_daily_metric_data_calculates_rates_and_counts() -> None:
    """重複率・カテゴリ別件数・文字数・推定トークンが計算されることを確認する。"""
    items = [
        _item(title="AIの記事", summary="概要です", category="ai_llm"),
        _item(title="経済の記事", summary=None, category="economy_finance"),
        _item(title="PR記事", summary="広告", category="unclassified", is_excluded=True),
    ]

    data = build_daily_metric_data(
        metric_date=METRIC_DATE_DAY2,
        total_fetched=4,
        deduped_items=items,
        rss_failure_rate=0.2,
    )

    assert data["metric_date"] == METRIC_DATE_DAY2
    assert data["is_actual_measurement"] is True
    assert data["total_fetched"] == 4
    assert data["deduped_count"] == 3
    assert data["duplicate_rate"] == 0.25
    assert data["rss_failure_rate"] == 0.2
    # 除外記事はカテゴリ件数・文字数・トークン見積りから除外する
    assert data["category_counts"]["ai_llm"] == 1
    assert data["category_counts"]["economy_finance"] == 1
    assert data["category_counts"]["unclassified"] == 0
    assert data["avg_title_chars"] == 5.0
    assert data["avg_summary_chars"] == 4.0
    assert data["estimated_input_tokens"] > 0


def test_build_daily_metric_data_handles_empty_collection() -> None:
    """取得0件でもゼロ除算せず、全項目0で返すことを確認する。"""
    data = build_daily_metric_data(
        metric_date=METRIC_DATE_DAY2,
        total_fetched=0,
        deduped_items=[],
    )

    assert data["duplicate_rate"] == 0.0
    assert data["avg_title_chars"] == 0.0
    assert data["avg_summary_chars"] == 0.0
    assert data["estimated_input_tokens"] == 0


def test_record_daily_metric_overwrites_same_date(db_session) -> None:
    """同一日の計測は加算せず、最新スナップショットで上書きすることを確認する。"""
    items_first = [_item(title="最初", summary="A")]
    items_second = [_item(title="更新後", summary="B"), _item(title="追加", summary="C")]

    record_daily_metric(db_session, METRIC_DATE_DAY2, 5, items_first)
    updated = record_daily_metric(db_session, METRIC_DATE_DAY2, 6, items_second)

    assert updated.total_fetched == 6
    assert updated.deduped_count == 2
    assert db_session.query(DailyRssMetric).count() == 1


# -----------------------------------------------------------------------------
# 2日分実測判定
# -----------------------------------------------------------------------------


def test_report_is_provisional_after_one_day(db_session) -> None:
    """1日分だけでは実測完了にならず、暫定表示になることを確認する。"""
    record_daily_metric(
        db_session,
        METRIC_DATE_DAY1,
        total_fetched=10,
        deduped_items=[_item(title="記事", summary="概要")],
    )

    report = get_phase2_report(db_session)

    assert report["is_actual_measurement"] is False
    assert report["actual_days"] == 1
    assert report["remaining_days"] == 1
    assert report["status_label"] == "暫定（1/2日）"
    assert "暫定" in report["measurement_note"]


def test_report_is_complete_after_two_days(db_session) -> None:
    """異なる2日分の実測が揃うと実測完了になることを確認する。"""
    record_daily_metric(
        db_session,
        METRIC_DATE_DAY1,
        total_fetched=10,
        deduped_items=[
            _item(title="AI記事", summary="概要", category="ai_llm"),
            _item(title="経済記事", summary="概要", category="economy_finance"),
        ],
        rss_failure_rate=0.0,
    )
    record_daily_metric(
        db_session,
        METRIC_DATE_DAY2,
        total_fetched=20,
        deduped_items=[
            _item(title="AI記事2", summary="概要2", category="ai_llm"),
        ],
        rss_failure_rate=0.5,
    )

    report = get_phase2_report(db_session)

    assert report["is_actual_measurement"] is True
    assert report["actual_days"] == 2
    assert report["remaining_days"] == 0
    assert report["status_label"] == "実測完了（2日分）"
    assert report["total_fetched"] == 30
    assert report["deduped_count"] == 3
    assert report["duplicate_rate"] == 0.9
    assert report["category_counts"]["ai_llm"] == 2
    assert report["category_counts"]["economy_finance"] == 1
    assert len(report["daily_metrics"]) == 2


def test_report_without_data_shows_unmeasured(db_session) -> None:
    """計測データが無い場合は未計測として表示することを確認する。"""
    report = get_phase2_report(db_session)

    assert report["is_actual_measurement"] is False
    assert report["actual_days"] == 0
    assert report["status_label"] == "未計測"
    assert report["daily_metrics"] == []


# -----------------------------------------------------------------------------
# RSSのみの計測実行
# -----------------------------------------------------------------------------


def test_run_rss_measurement_saves_metric_without_articles(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """計測専用実行は記事を保存せず、日次計測値だけを保存することを確認する。"""
    items = [
        _item(title="AIニュース", summary="AIの概要", category="ai_llm"),
        _item(title="金融ニュース", summary="金融の概要", category="economy_finance"),
    ]

    def fake_collect(_payload: dict[str, object]) -> dict[str, object]:
        return _fake_collected(items, failure_rate=0.25)

    monkeypatch.setattr(metrics_service, "collect_rss", fake_collect)

    result = run_rss_measurement(
        session=db_session,
        metric_date=METRIC_DATE_DAY2,
    )

    assert result["metric"]["total_fetched"] == 2
    assert result["metric"]["rss_failure_rate"] == 0.25
    assert result["report"]["actual_days"] == 1
    assert db_session.query(NewsArticle).count() == 0
    assert db_session.query(DailyRssMetric).count() == 1


def test_digest_run_records_daily_metric(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """通常のニュース更新実行からもPhase 2の日次計測値が記録されることを確認する。"""
    items = [
        {
            "source_id": 1,
            "source_name": "テストメディア",
            "guid": "metric-1",
            "url": "https://example.com/metric-1",
            "normalized_url": "https://example.com/metric-1",
            "title": "計測対象の記事",
            "published_at": None,
            "updated_at_rss": None,
            "is_pubdate_verified": False,
            "rss_categories": [],
            "summary_snippet": "概要",
            "primary_category": "ai_llm",
            "importance_score": 60,
            "is_excluded": False,
            "exclusion_reason": None,
        }
    ]

    def fake_collect(_payload: dict[str, object]) -> dict[str, object]:
        return _fake_collected(items)

    monkeypatch.setattr(digest_orchestrator, "collect_rss", fake_collect)

    result = digest_orchestrator.execute_digest_run(
        session=db_session,
        run_type="manual",
    )

    assert result["status"] == "completed"
    metrics = MetricsRepository(db_session).list_metrics()
    assert len(metrics) == 1
    assert metrics[0].total_fetched == 1
    assert metrics[0].category_counts["ai_llm"] == 1


# -----------------------------------------------------------------------------
# 計測レポートAPI・画面
# -----------------------------------------------------------------------------


def test_metrics_page_shows_unmeasured(client) -> None:
    """計測データが無い場合に計測画面が未計測表示になることを確認する。"""
    response = client.get("/metrics")

    assert response.status_code == 200
    assert "RSS取得量計測（Phase 2）" in response.text
    assert "未計測" in response.text


def test_metrics_api_returns_report(client) -> None:
    """計測レポートAPIが2日未満の暫定状態をJSONで返すことを確認する。"""
    response = client.get("/api/metrics")

    assert response.status_code == 200
    payload = response.json()
    assert payload["is_actual_measurement"] is False
    assert payload["actual_days"] == 0
    assert payload["status_label"] == "未計測"
    assert payload["daily_metrics"] == []


def test_metrics_measure_api_records_and_reflects_report(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """計測APIがRSSのみを実行し、画面とレポートへ1日分の実測を反映することを確認する。"""
    items = [
        _item(title="AIニュース", summary="AIの概要", category="ai_llm"),
        _item(title="金融ニュース", summary="金融の概要", category="economy_finance"),
    ]

    def fake_collect(_payload: dict[str, object]) -> dict[str, object]:
        return _fake_collected(items, failure_rate=0.5)

    monkeypatch.setattr(metrics_service, "collect_rss", fake_collect)

    response = client.post("/api/metrics/measure", json={})

    assert response.status_code == 200
    payload = response.json()
    assert payload["metric"]["total_fetched"] == 2
    assert payload["report"]["actual_days"] == 1
    assert payload["report"]["is_actual_measurement"] is False

    api_report = client.get("/api/metrics").json()
    assert api_report["actual_days"] == 1
    assert api_report["status_label"] == "暫定（1/2日）"
    assert api_report["category_counts"]["ai_llm"] == 1

    page = client.get("/metrics")
    assert page.status_code == 200
    assert "暫定（1/2日）" in page.text
    assert "AIの概要" not in page.text


def test_metrics_page_shows_complete_after_two_days(client, db_session) -> None:
    """2日分の計測が揃うと画面が実測完了表示になることを確認する。"""
    record_daily_metric(
        db_session,
        METRIC_DATE_DAY1,
        total_fetched=5,
        deduped_items=[_item(title="1日目", summary="概要")],
    )
    record_daily_metric(
        db_session,
        METRIC_DATE_DAY2,
        total_fetched=8,
        deduped_items=[_item(title="2日目", summary="概要")],
    )

    response = client.get("/metrics")

    assert response.status_code == 200
    assert "実測完了（2日分）" in response.text
    assert "2026-11-02" in response.text
    assert "13件" in response.text
