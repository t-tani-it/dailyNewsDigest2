"""Phase 1: RSS取得・重複排除・更新検知・対象日フィルタリングのテスト。

目的（Why）:
    Phase 1 の心臓部である「RSSから全件取得する」「同じ記事を二重登録しない」
    「既存記事の更新を検知する」「日本時間の対象日で正しく絞り込む」という
    4つの振る舞いが壊れていないことを自動検証する。
仕様（What）:
    - `fetch_single_feed`: 成功・HTTP失敗・利用条件未確認の3パターン
    - `dedup_service`: 正規化URL重複・同一GUID重複・別メディア同一イベントのクラスタリング
    - `ArticleRepository.upsert_articles`: 新規・重複・更新の判定
    - `filter_items_by_target_date`: JST日境界・公開日時不明記事・範囲外通知
前提条件・依存関係（Assumption / Dependencies）:
    pytest, httpx.MockTransport, `app.services`, `app.repositories`
入出力（I/O）:
    テスト関数ごとに assert で合否を判定する。
注意点（Caution）:
    実ネットワークへは接続しない。HTTP通信は必ず `httpx.MockTransport` で差し替える。
今後の拡張ポイント（Future Work）:
    PostgreSQL 固有のタイムゾーン挙動を確認する結合テスト。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 2-5）
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import httpx
import pytest

from app.models.article import NewsArticle
from app.models.rss_source import RssSource
from app.repositories.article_repo import ArticleRepository
from app.services.dedup_service import main as run_dedup
from app.services.rss_service import (
    fetch_single_feed,
    filter_items_by_target_date,
    get_jst_day_utc_bounds,
    parse_rss_xml_content,
)

# =============================================================================
# 設定値ブロック（テスト用RSS/Atomサンプル）
# =============================================================================

SAMPLE_RSS_XML: str = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>テスト用ニュース</title>
    <link>https://example.com/</link>
    <description>テストフィード</description>
    <item>
      <title>OpenAIが新しい生成AIモデルを発表</title>
      <link>https://example.com/news/1?utm_source=rss</link>
      <guid>g-1</guid>
      <pubDate>Mon, 02 Nov 2026 00:00:00 +0900</pubDate>
      <description><![CDATA[<p>新しいAIエージェントの機能を公開。</p><script>alert(1)</script>]]></description>
      <category>AI</category>
    </item>
    <item>
      <title>【PR】ノートPCのタイムセール</title>
      <link>https://example.com/pr/2</link>
      <guid>g-2</guid>
      <pubDate>Mon, 02 Nov 2026 01:00:00 +0900</pubDate>
      <description>広告記事です</description>
    </item>
  </channel>
</rss>
"""


def _mock_http_client(xml_text: str, status_code: int = 200) -> httpx.Client:
    """固定レスポンスを返す `httpx.Client` を生成する（実通信なし）。

    Args:
        xml_text (str): レスポンス本文。
        status_code (int): HTTPステータスコード。

    Returns:
        httpx.Client: MockTransport を組み込んだクライアント。

    Side Effects:
        なし。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text=xml_text, request=request)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _make_item(
    *,
    title: str,
    url: str,
    source_name: str = "テストメディア",
    guid: str | None = None,
    published_at: datetime | None = None,
    summary: str | None = "概要",
    category: str = "ai_llm",
) -> dict[str, object]:
    """テスト用の正規化済み記事辞書を生成する。

    Args:
        title (str): 記事タイトル。
        url (str): 正規化済みURL。
        source_name (str): 配信元名。
        guid (str | None): RSS GUID。
        published_at (datetime | None): 公開日時（UTC）。
        summary (str | None): 概要。
        category (str): 一次分類カテゴリ。

    Returns:
        dict[str, object]: 記事辞書。

    Side Effects:
        なし。
    """
    return {
        "source_id": 1,
        "source_name": source_name,
        "guid": guid or url,
        "url": url,
        "normalized_url": url,
        "title": title,
        "published_at": published_at or datetime(2026, 11, 1, 15, 0, tzinfo=UTC),
        "updated_at_rss": None,
        "is_pubdate_verified": published_at is not None,
        "rss_categories": [],
        "summary_snippet": summary,
        "primary_category": category,
        "importance_score": 60,
        "is_excluded": False,
        "exclusion_reason": None,
    }


# -----------------------------------------------------------------------------
# RSS取得
# -----------------------------------------------------------------------------


def test_fetch_single_feed_success_parses_and_sanitizes() -> None:
    """RSS取得成功時に全記事を取得し、HTMLが無害化されることを確認する。"""
    source = RssSource(
        id=1,
        name="テストメディア",
        url="https://example.com/rss.xml",
        is_enabled=True,
        terms_status="verified",
        allow_store_summary=True,
    )
    client = _mock_http_client(SAMPLE_RSS_XML)
    try:
        result = fetch_single_feed(source=source, http_client=client)
    finally:
        client.close()

    assert result["success"] is True
    items = result["items"]
    assert len(items) == 2

    ai_item = items[0]
    assert ai_item["title"] == "OpenAIが新しい生成AIモデルを発表"
    assert ai_item["normalized_url"] == "https://example.com/news/1"
    assert ai_item["primary_category"] == "ai_llm"
    assert ai_item["is_excluded"] is False
    assert "<script>" not in str(ai_item["summary_snippet"])
    assert "alert(1)" not in str(ai_item["summary_snippet"])

    pr_item = items[1]
    assert pr_item["is_excluded"] is True
    assert pr_item["exclusion_reason"] == "advertisement_pr"


def test_fetch_single_feed_http_error_returns_failure() -> None:
    """RSS取得がHTTP 503の場合は success=False とエラーメッセージを返すことを確認する。"""
    source = RssSource(
        id=2,
        name="障害中メディア",
        url="https://example.com/rss.xml",
        is_enabled=True,
        terms_status="verified",
        allow_store_summary=True,
    )
    client = _mock_http_client("Service Unavailable", status_code=503)
    try:
        result = fetch_single_feed(source=source, http_client=client)
    finally:
        client.close()

    assert result["success"] is False
    assert result["items"] == []
    assert result["error"]


def test_fetch_single_feed_rejects_unverified_source() -> None:
    """利用条件が未確認（unverified）の情報源は取得しないことを確認する。"""
    source = RssSource(
        id=3,
        name="未確認メディア",
        url="https://example.com/rss.xml",
        is_enabled=True,
        terms_status="unverified",
        allow_store_summary=True,
    )
    result = fetch_single_feed(source=source, http_client=_mock_http_client(SAMPLE_RSS_XML))

    assert result["success"] is False
    assert result["items"] == []
    assert "利用条件未確認" in str(result["error"])


def test_parse_rss_xml_content_keeps_all_entries() -> None:
    """RSS解析段階では15件に絞らず、公開されている全エントリを保持することを確認する。"""
    items = parse_rss_xml_content(
        xml_content=SAMPLE_RSS_XML,
        source_name="テストメディア",
        source_id=1,
        allow_store_summary=True,
    )

    assert len(items) == 2


# -----------------------------------------------------------------------------
# 重複排除・関連記事クラスタリング
# -----------------------------------------------------------------------------


def test_dedup_removes_same_normalized_url_and_guid() -> None:
    """正規化URLや同一配信元GUIDが同じ記事は重複として除外されることを確認する。"""
    item_a = _make_item(
        title="日銀が政策金利を据え置き決定",
        url="https://example.com/news/1",
        guid="g-1",
    )
    item_b = _make_item(
        title="日銀が政策金利を据え置き決定",
        url="https://example.com/news/1",
        guid="g-1",
    )

    result = run_dedup([item_a, item_b])

    assert result["total_count"] == 2
    assert result["deduped_count"] == 1
    assert result["duplicate_count"] == 1


def test_dedup_clusters_same_event_from_different_sources() -> None:
    """別メディアが同じ出来事を報じた場合は削除せず同じ cluster_id を付与することを確認する。"""
    item_a = _make_item(
        title="日銀が政策金利の据え置きを決定",
        url="https://media-a.example.com/news/1",
        source_name="メディアA",
        guid="a-1",
    )
    item_b = _make_item(
        title="日銀、政策金利の据え置きを決定",
        url="https://media-b.example.com/news/2",
        source_name="メディアB",
        guid="b-1",
    )

    result = run_dedup([item_a, item_b])

    assert result["deduped_count"] == 2
    first, second = result["deduped_items"]
    assert first["cluster_id"] == second["cluster_id"]


def test_dedup_keeps_distinct_articles() -> None:
    """類似度が低い無関係な記事は重複排除されないことを確認する。"""
    item_a = _make_item(
        title="OpenAIが新型LLMを発表",
        url="https://example.com/ai",
        source_name="メディアA",
    )
    item_b = _make_item(
        title="日銀が金融政策決定会合を開催",
        url="https://example.com/finance",
        source_name="メディアB",
        category="economy_finance",
    )

    result = run_dedup([item_a, item_b])

    assert result["deduped_count"] == 2
    assert result["duplicate_count"] == 0


# -----------------------------------------------------------------------------
# 記事の新規登録・更新検知
# -----------------------------------------------------------------------------


def test_upsert_articles_detects_new_duplicate_and_update(db_session) -> None:
    """記事の新規登録・完全重複・内容更新を正しく判定することを確認する。"""
    repo = ArticleRepository(db_session)
    item = _make_item(
        title="OpenAIが新型AIエージェントを公開",
        url="https://example.com/news/ai-1",
        guid="ai-1",
        summary="最初の概要",
    )

    first = repo.upsert_articles([item])
    assert first["new_count"] == 1
    assert first["updated_count"] == 0
    assert first["duplicate_count"] == 0

    second = repo.upsert_articles([dict(item)])
    assert second["new_count"] == 0
    assert second["updated_count"] == 0
    assert second["duplicate_count"] == 1

    updated_item = dict(item)
    updated_item["summary_snippet"] = "更新された概要"
    third = repo.upsert_articles([updated_item])
    assert third["new_count"] == 0
    assert third["updated_count"] == 1

    stored = repo.find_by_normalized_url("https://example.com/news/ai-1")
    assert stored is not None
    assert stored.is_updated_article is True
    assert stored.summary_snippet == "更新された概要"


def test_upsert_articles_does_not_reinsert_same_guid(db_session) -> None:
    """URLが変わっても同一配信元のGUIDが同じなら重複として扱うことを確認する。"""
    repo = ArticleRepository(db_session)
    item = _make_item(
        title="同一記事のURL変更",
        url="https://example.com/news/old",
        guid="same-guid",
    )
    repo.upsert_articles([item])

    moved = dict(item)
    moved["url"] = "https://example.com/news/new"
    moved["normalized_url"] = "https://example.com/news/new"
    result = repo.upsert_articles([moved])

    assert result["new_count"] == 0
    assert result["duplicate_count"] == 1
    assert db_session.query(NewsArticle).count() == 1


# -----------------------------------------------------------------------------
# 日本時間の対象日フィルタリング
# -----------------------------------------------------------------------------


def test_jst_day_bounds_are_utc_converted() -> None:
    """JST対象日の0:00〜翌日0:00がUTCの15:00〜翌15:00へ変換されることを確認する。"""
    start_utc, end_utc = get_jst_day_utc_bounds(date(2026, 11, 2))

    assert start_utc == datetime(2026, 11, 1, 15, 0, tzinfo=UTC)
    assert end_utc == datetime(2026, 11, 2, 15, 0, tzinfo=UTC)
    assert (end_utc - start_utc).total_seconds() == 86400


def test_filter_items_by_target_date_uses_jst_boundaries() -> None:
    """JSTの日付境界ちょうどは含み、翌日0:00ちょうどは除外することを確認する。"""
    start_item = _make_item(
        title="開始ちょうどの記事",
        url="https://example.com/start",
        published_at=datetime(2026, 11, 1, 15, 0, tzinfo=UTC),
    )
    end_item = _make_item(
        title="終了ちょうどの記事",
        url="https://example.com/end",
        published_at=datetime(2026, 11, 2, 15, 0, tzinfo=UTC),
    )
    noon_item = _make_item(
        title="対象日の昼の記事",
        url="https://example.com/noon",
        published_at=datetime(2026, 11, 2, 3, 0, tzinfo=UTC),
    )

    result = filter_items_by_target_date([start_item, end_item, noon_item], date(2026, 11, 2))

    titles = [item["title"] for item in result["matched_items"]]
    assert "開始ちょうどの記事" in titles
    assert "対象日の昼の記事" in titles
    assert "終了ちょうどの記事" not in titles
    assert result["out_of_range_notice"] is None


def test_filter_items_by_target_date_excludes_unverified_pubdate() -> None:
    """公開日時が不明な記事を対象日内と断定せず除外することを確認する。"""
    item = _make_item(
        title="日時不明の記事",
        url="https://example.com/unknown-date",
    )
    item["published_at"] = None
    item["is_pubdate_verified"] = False

    result = filter_items_by_target_date([item], date(2026, 11, 2))

    assert result["matched_items"] == []
    assert result["unverified_pubdate_count"] == 1
    assert result["out_of_range_notice"] is not None
    assert "存在しません" in str(result["out_of_range_notice"])


def test_filter_items_by_target_date_reports_available_range() -> None:
    """対象日外の記事しかない場合にRSSの公開日付範囲を通知することを確認する。"""
    old_item = _make_item(
        title="古い記事",
        url="https://example.com/old",
        published_at=datetime(2026, 10, 1, 0, 0, tzinfo=UTC),
    )
    new_item = _make_item(
        title="新しい記事",
        url="https://example.com/new",
        published_at=datetime(2026, 10, 3, 0, 0, tzinfo=UTC),
    )

    result = filter_items_by_target_date([old_item, new_item], date(2026, 11, 2))

    assert result["matched_items"] == []
    assert "2026-10-01" in str(result["out_of_range_notice"])
    assert "2026-10-03" in str(result["out_of_range_notice"])


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://example.com/a/?utm_source=rss&b=2&a=1#section", "https://example.com/a?a=1&b=2"),
        ("HTTPS://Example.com:443/news/", "https://example.com/news"),
    ],
)
def test_normalize_url_removes_tracking_and_sorts_query(value: str, expected: str) -> None:
    """URL正規化がトラッキング除去・クエリ整列・末尾スラッシュ統一を行うことを確認する。"""
    from app.core.security import normalize_url

    assert normalize_url(value) == expected
