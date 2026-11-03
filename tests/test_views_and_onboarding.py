"""Phase 1: 初回確認フロー・画面表示・最大15件選定・実行履歴・補完取得のテスト。

目的（Why）:
    「取得していいですか？」の確認前後にDB保存やAI処理が正しく分岐すること、
    ニュースが最大15件に収まること、並び順・既読状態・実行履歴が画面とAPIに
    正しく反映されることを自動検証する。
仕様（What）:
    - 初回確認: はい（保存・自動更新有効） / いいえ（保存なし） / キャンセル（破棄）
    - 選定: 最大15件・表示件数指定・カテゴリOR条件・重要度順/最新順
    - 既読切替API、実行履歴API・履歴画面、停止後補完取得
前提条件・依存関係（Assumption / Dependencies）:
    pytest, `app.services.digest_orchestrator`, `app.repositories`, `app.services.selector_service`
入出力（I/O）:
    テスト関数ごとに assert で合否を判定する。
注意点（Caution）:
    RSSの外部通信は `monkeypatch` で必ず差し替え、テストをネットワーク非依存に保つ。
今後の拡張ポイント（Future Work）:
    スマートフォン表示幅を想定したE2Eテスト（Playwright等）の追加。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 2-5）
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.models.article import NewsArticle
from app.repositories.article_repo import ArticleRepository
from app.repositories.run_repo import RunRepository
from app.repositories.setting_repo import SettingRepository
from app.services import digest_orchestrator
from app.services.digest_orchestrator import (
    clear_preview_cache,
    handle_onboarding_decision,
    prepare_onboarding_preview,
    run_catchup_if_needed,
)
from app.services.selector_service import select_top_articles, sort_articles

# =============================================================================
# 設定値ブロック（テスト用の固定日時・カテゴリ）
# =============================================================================

TARGET_DATE = date(2026, 11, 2)
BASE_PUBLISHED_AT = datetime(2026, 11, 1, 15, 30, tzinfo=UTC)
DEFAULT_CATEGORIES = ["ai_llm", "economy_finance"]


def _make_item(
    *,
    title: str,
    url: str,
    category: str = "ai_llm",
    source_name: str = "テストメディア",
    importance: int = 60,
    summary: str = "テスト概要",
) -> dict[str, object]:
    """テスト用のRSS記事辞書を生成する。"""
    return {
        "source_id": 1,
        "source_name": source_name,
        "guid": url,
        "url": url,
        "normalized_url": url,
        "title": title,
        "published_at": BASE_PUBLISHED_AT,
        "updated_at_rss": None,
        "is_pubdate_verified": True,
        "rss_categories": [],
        "summary_snippet": summary,
        "primary_category": category,
        "importance_score": importance,
        "is_excluded": False,
        "exclusion_reason": None,
    }


def _fake_collection(items: list[dict[str, object]]) -> dict[str, object]:
    """`collect_rss` の戻り値互換の辞書を生成する。"""
    return {
        "all_fetched_items": items,
        "target_items": items,
        "unverified_pubdate_count": 0,
        "out_of_range_notice": None,
        "source_results": [
            {
                "source_id": 1,
                "source_name": "テストメディア",
                "success": True,
                "fetched_count": len(items),
                "error": None,
            }
        ],
        "success_sources": 1,
        "failed_sources": 0,
        "rss_failure_rate": 0.0,
    }


@pytest.fixture()
def sample_items() -> list[dict[str, object]]:
    """初回確認・更新テストで使う記事3件を返す。"""
    pr_item = _make_item(
        title="【PR】週末セールのお知らせ",
        url="https://example.com/news/pr-1",
        category="unclassified",
        importance=10,
    )
    pr_item["is_excluded"] = True
    pr_item["exclusion_reason"] = "advertisement_pr"
    return [
        _make_item(
            title="OpenAIが新型AIエージェントを発表",
            url="https://example.com/news/ai-1",
            category="ai_llm",
            importance=80,
        ),
        _make_item(
            title="日銀が政策金利を据え置き",
            url="https://example.com/news/finance-1",
            category="economy_finance",
            source_name="経済メディア",
            importance=70,
        ),
        pr_item,
    ]


def _patch_collection(monkeypatch: pytest.MonkeyPatch, items: list[dict[str, object]]) -> None:
    """オーケストレータ内の `collect_rss` を固定結果へ差し替える。"""

    def fake_collect(_payload: dict[str, object]) -> dict[str, object]:
        return _fake_collection(items)

    monkeypatch.setattr(digest_orchestrator, "collect_rss", fake_collect)


# -----------------------------------------------------------------------------
# 初回確認フロー
# -----------------------------------------------------------------------------


def test_index_shows_onboarding_before_approval(client) -> None:
    """初回未承認時はトップページに「取得していいですか？」が表示されることを確認する。"""
    response = client.get("/")

    assert response.status_code == 200
    assert "取得していいですか？" in response.text
    assert "有料APIは実行しないこと" in response.text


def test_onboarding_preview_does_not_save_articles(
    db_session, monkeypatch: pytest.MonkeyPatch, sample_items: list[dict[str, object]]
) -> None:
    """確認前のプレビュー段階では記事をDBへ保存しないことを確認する。"""
    clear_preview_cache()
    _patch_collection(monkeypatch, sample_items)

    preview = prepare_onboarding_preview(
        session=db_session,
        target_date=TARGET_DATE,
        selected_categories=DEFAULT_CATEGORIES,
    )

    assert preview.preview_token
    assert preview.total_fetched_count == 3
    assert preview.deduped_count == 3
    assert db_session.query(NewsArticle).count() == 0


def test_onboarding_no_decision_keeps_titles_only(
    db_session, monkeypatch: pytest.MonkeyPatch, sample_items: list[dict[str, object]]
) -> None:
    """「いいえ」選択時はDB保存もAI処理も行わず、タイトルのみ返すことを確認する。"""
    clear_preview_cache()
    _patch_collection(monkeypatch, sample_items)
    preview = prepare_onboarding_preview(
        session=db_session,
        target_date=TARGET_DATE,
        selected_categories=DEFAULT_CATEGORIES,
    )

    result = handle_onboarding_decision(
        session=db_session,
        preview_token=preview.preview_token,
        decision="no",
    )

    assert result["db_saved"] is False
    assert result["ai_executed"] is False
    # プレビュー一覧は除外記事（PR）を除いた2件を返す
    assert len(result["preview_titles"]) == 2
    assert db_session.query(NewsArticle).count() == 0


def test_onboarding_cancel_discards_preview(
    db_session, monkeypatch: pytest.MonkeyPatch, sample_items: list[dict[str, object]]
) -> None:
    """「キャンセル」選択時はプレビューを破棄し設定画面へ戻すことを確認する。"""
    clear_preview_cache()
    _patch_collection(monkeypatch, sample_items)
    preview = prepare_onboarding_preview(
        session=db_session,
        target_date=TARGET_DATE,
        selected_categories=DEFAULT_CATEGORIES,
    )

    result = handle_onboarding_decision(
        session=db_session,
        preview_token=preview.preview_token,
        decision="cancel",
    )

    assert result["status"] == "cancelled"
    assert result["db_saved"] is False
    assert result["redirect_to"] == "/settings"


def test_onboarding_yes_saves_articles_and_enables_auto_update(
    db_session, monkeypatch: pytest.MonkeyPatch, sample_items: list[dict[str, object]]
) -> None:
    """「はい」選択時は記事保存・初回承認・自動更新有効化・実行履歴作成を行うことを確認する。"""
    clear_preview_cache()
    _patch_collection(monkeypatch, sample_items)
    preview = prepare_onboarding_preview(
        session=db_session,
        target_date=TARGET_DATE,
        selected_categories=DEFAULT_CATEGORIES,
    )

    result = handle_onboarding_decision(
        session=db_session,
        preview_token=preview.preview_token,
        decision="yes",
        selected_categories=DEFAULT_CATEGORIES,
    )

    assert result["db_saved"] is True
    assert result["ai_executed"] is False

    setting = SettingRepository(db_session).get_or_create()
    assert setting.is_initial_approved is True
    assert setting.auto_update_enabled is True

    # 除外記事（PR）も履歴再現用にDBへ保存されるが、表示選定からは外れる
    assert db_session.query(NewsArticle).count() == 3

    run = RunRepository(db_session).get_run(str(result["run_id"]))
    assert run is not None
    assert run.displayed_count == 2
    assert run.fetched_count == 3


def test_approved_index_displays_news_cards(
    client, db_session, monkeypatch: pytest.MonkeyPatch, sample_items: list[dict[str, object]]
) -> None:
    """初回承認後はトップページに選定済みニュースカードが表示されることを確認する。"""
    clear_preview_cache()
    _patch_collection(monkeypatch, sample_items)
    preview = prepare_onboarding_preview(
        session=db_session,
        target_date=TARGET_DATE,
        selected_categories=DEFAULT_CATEGORIES,
    )
    handle_onboarding_decision(
        session=db_session,
        preview_token=preview.preview_token,
        decision="yes",
        selected_categories=DEFAULT_CATEGORIES,
    )

    response = client.get("/")

    assert response.status_code == 200
    assert "OpenAIが新型AIエージェントを発表" in response.text
    assert "重要ニュース一覧" in response.text


# -----------------------------------------------------------------------------
# 最大15件選定・カテゴリ・並び替え
# -----------------------------------------------------------------------------


def _make_candidates(count: int) -> list[dict[str, object]]:
    """重複クラスタのない候補記事を指定件数生成する。"""
    return [
        {
            "id": index,
            "title": f"候補記事 {index}",
            "source_name": f"メディア{index}",
            "primary_category": "ai_llm" if index % 3 else "economy_finance",
            "importance_score": 50 + (index % 40),
            "is_excluded": False,
            "published_at": BASE_PUBLISHED_AT - timedelta(minutes=index),
            "cluster_id": f"cluster-{index}",
        }
        for index in range(1, count + 1)
    ]


def test_select_top_articles_caps_at_15() -> None:
    """候補が15件を超えても選定結果は最大15件であることを確認する。"""
    selected = select_top_articles(_make_candidates(20), max_count=15)

    assert len(selected) == 15


def test_select_top_articles_respects_display_count() -> None:
    """表示件数指定（1〜15件）が選定結果に反映されることを確認する。"""
    selected = select_top_articles(_make_candidates(20), max_count=5)

    assert len(selected) == 5


def test_select_top_articles_excludes_pr_and_filters_categories() -> None:
    """除外記事を選ばず、選択カテゴリ（OR条件）の記事だけを選ぶことを確認する。"""
    candidates = [
        {
            "id": 1,
            "title": "AI記事",
            "source_name": "A",
            "primary_category": "ai_llm",
            "importance_score": 90,
            "is_excluded": False,
            "published_at": BASE_PUBLISHED_AT,
            "cluster_id": "c1",
        },
        {
            "id": 2,
            "title": "金融記事",
            "source_name": "B",
            "primary_category": "economy_finance",
            "importance_score": 80,
            "is_excluded": False,
            "published_at": BASE_PUBLISHED_AT,
            "cluster_id": "c2",
        },
        {
            "id": 3,
            "title": "PR記事",
            "source_name": "C",
            "primary_category": "ai_llm",
            "importance_score": 100,
            "is_excluded": True,
            "published_at": BASE_PUBLISHED_AT,
            "cluster_id": "c3",
        },
        {
            "id": 4,
            "title": "科学記事",
            "source_name": "D",
            "primary_category": "science_tech",
            "importance_score": 95,
            "is_excluded": False,
            "published_at": BASE_PUBLISHED_AT,
            "cluster_id": "c4",
        },
    ]

    selected = select_top_articles(
        candidates,
        selected_categories=["ai_llm", "economy_finance"],
        max_count=15,
    )

    titles = [item["title"] for item in selected]
    assert "AI記事" in titles
    assert "金融記事" in titles
    assert "PR記事" not in titles
    assert "科学記事" not in titles


def test_select_top_articles_shows_one_article_per_cluster() -> None:
    """同じ出来事の関連記事は代表1件だけを表示することを確認する。"""
    candidates = [
        {
            "id": 1,
            "title": "日銀が金利を据え置き（A社）",
            "source_name": "A",
            "primary_category": "economy_finance",
            "importance_score": 70,
            "is_excluded": False,
            "published_at": BASE_PUBLISHED_AT,
            "cluster_id": "same-event",
        },
        {
            "id": 2,
            "title": "日銀が金利を据え置き（B社）",
            "source_name": "B",
            "primary_category": "economy_finance",
            "importance_score": 90,
            "is_excluded": False,
            "published_at": BASE_PUBLISHED_AT,
            "cluster_id": "same-event",
        },
    ]

    selected = select_top_articles(
        candidates,
        selected_categories=["economy_finance"],
        max_count=15,
    )

    assert len(selected) == 1
    assert selected[0]["id"] == 2


def test_sort_articles_importance_and_latest() -> None:
    """重要度順と最新順の並び替えがそれぞれ正しいことを確認する。"""
    older_important = {
        "id": 1,
        "title": "古いが重要",
        "importance_score": 95,
        "published_at": datetime(2026, 11, 1, 0, 0, tzinfo=UTC),
    }
    newer_unimportant = {
        "id": 2,
        "title": "新しいが重要度低",
        "importance_score": 40,
        "published_at": datetime(2026, 11, 2, 0, 0, tzinfo=UTC),
    }

    by_importance = sort_articles([newer_unimportant, older_important], "importance")
    by_latest = sort_articles([older_important, newer_unimportant], "latest")

    assert by_importance[0]["id"] == 1
    assert by_latest[0]["id"] == 2


# -----------------------------------------------------------------------------
# 手動更新・既読・実行履歴
# -----------------------------------------------------------------------------


def test_manual_refresh_api_and_run_history(
    client, db_session, monkeypatch: pytest.MonkeyPatch, sample_items: list[dict[str, object]]
) -> None:
    """手動更新APIが実行履歴を残し、履歴API・履歴画面から参照できることを確認する。"""
    SettingRepository(db_session).approve_initial_onboarding(DEFAULT_CATEGORIES)
    _patch_collection(monkeypatch, sample_items)

    refresh_response = client.post(
        "/api/news/refresh",
        json={"selected_categories": DEFAULT_CATEGORIES},
    )
    assert refresh_response.status_code == 200
    payload = refresh_response.json()
    run_id = payload["run_result"]["run_id"]
    assert payload["run_result"]["displayed_count"] == 2
    assert len(payload["cards"]) == 2

    list_response = client.get("/api/runs")
    assert list_response.status_code == 200
    runs = list_response.json()
    assert len(runs) == 1
    assert runs[0]["run_id"] == run_id

    detail_response = client.get(f"/api/runs/{run_id}")
    assert detail_response.status_code == 200
    assert len(detail_response.json()["cards"]) == 2

    history_response = client.get(f"/history?run_id={run_id}")
    assert history_response.status_code == 200
    assert "OpenAIが新型AIエージェントを発表" in history_response.text


def test_article_read_status_api(client, db_session) -> None:
    """既読・未読切替APIがDBとレスポンスへ反映されることを確認する。"""
    repo = ArticleRepository(db_session)
    created = repo.upsert_articles(
        [
            {
                "source_id": 1,
                "source_name": "テストメディア",
                "guid": "read-1",
                "url": "https://example.com/news/read-1",
                "normalized_url": "https://example.com/news/read-1",
                "title": "既読テスト記事",
                "published_at": BASE_PUBLISHED_AT,
                "is_pubdate_verified": True,
                "rss_categories": [],
                "summary_snippet": "概要",
                "primary_category": "ai_llm",
                "importance_score": 60,
                "is_excluded": False,
            }
        ]
    )
    article_id = created["articles"][0].id

    response = client.post(f"/api/articles/{article_id}/read", json={"is_read": True})

    assert response.status_code == 200
    assert response.json() == {"id": article_id, "is_read": True}

    # リクエスト処理は別セッションのため、テスト側セッションのキャッシュを破棄して再読込する
    db_session.expire_all()
    stored = ArticleRepository(db_session).get_by_id(article_id)
    assert stored is not None
    assert stored.is_read is True


def test_catchup_runs_when_scheduled_time_missed(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """自動更新時刻を過ぎて未実行の場合に補完取得が実行されることを確認する。"""
    SettingRepository(db_session).approve_initial_onboarding(DEFAULT_CATEGORIES)
    called: dict[str, object] = {}

    def fake_execute(**kwargs: object) -> dict[str, object]:
        called.update(kwargs)
        return {"run_id": "catchup-run", "displayed_count": 4}

    monkeypatch.setattr(digest_orchestrator, "execute_digest_run", fake_execute)

    result = run_catchup_if_needed(
        session=db_session,
        now_utc=datetime(2026, 11, 2, 0, 0, tzinfo=UTC),
    )

    assert result["catchup_executed"] is True
    assert called.get("run_type") == "catchup"


def test_catchup_skips_when_already_succeeded_today(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """当日の自動更新時刻以降に成功済みの場合は補完取得しないことを確認する。"""
    setting = SettingRepository(db_session).approve_initial_onboarding(DEFAULT_CATEGORIES)
    setting.last_successful_run_at = datetime(2026, 11, 2, 1, 0, tzinfo=UTC)
    db_session.commit()

    called = {"executed": False}

    def fake_execute(**_kwargs: object) -> dict[str, object]:
        called["executed"] = True
        return {"run_id": "should-not-run"}

    monkeypatch.setattr(digest_orchestrator, "execute_digest_run", fake_execute)

    result = run_catchup_if_needed(
        session=db_session,
        now_utc=datetime(2026, 11, 2, 2, 0, tzinfo=UTC),
    )

    assert result["catchup_executed"] is False
    assert called["executed"] is False


def test_run_lock_prevents_double_execution(
    db_session, monkeypatch: pytest.MonkeyPatch, sample_items: list[dict[str, object]]
) -> None:
    """別プロセスがロック中の場合は二重実行せず locked を返すことを確認する。"""
    setting_repo = SettingRepository(db_session)
    setting_repo.approve_initial_onboarding(DEFAULT_CATEGORIES)
    assert setting_repo.acquire_run_lock("another-run-id") is True

    _patch_collection(monkeypatch, sample_items)
    result = digest_orchestrator.execute_digest_run(
        session=db_session,
        run_type="manual",
        selected_categories=DEFAULT_CATEGORIES,
    )

    assert result["status"] == "locked"
    assert result["displayed_count"] == 0
