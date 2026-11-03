"""RSS/Atom ニュース収集サービス。

目的（Why）:
    利用条件が確認された公式RSS/Atomフィードから現在公開されている新着・更新記事を全件取得し、
    SSRF対策・HTML無害化・日本時間（JST）基準の対象日判定を行って後続処理へ渡す（指示書 §5, §6.1）。
仕様（What）:
    - `fetch_single_feed`: SSRF検証付きHTTP通信と `feedparser` による全件解析
    - `get_jst_day_utc_bounds`: 日本時間の対象日（0:00〜翌日0:00直前）をUTC範囲へ変換
    - `filter_items_by_target_date`: 公開日時不明記事を誤って対象日内と断定しないフィルタリングと範囲外検知
    - `collect_from_sources`: 有効かつ `terms_status == "verified"` の情報源のみを巡回収集
前提条件・依存関係（Assumption / Dependencies）:
    feedparser>=6.0, httpx>=0.27, `app.core.config`, `app.core.security`, `app.services.filter_service`
入出力（I/O）:
    入力: `RssSource` リスト、対象日（`date`）、概要文字数制限
    出力: 収集記事リスト、取得成功・失敗統計、公開範囲外通知メッセージ
注意点（Caution）:
    - RSSから取得できる全記事を対象とし、この段階で15件に絞り込んではならない（指示書 §5.1）。
    - `terms_status != "verified"` の情報源は自動収集から除外する（指示書 §5.3）。
今後の拡張ポイント（Future Work）:
    If-Modified-Since / ETag を用いた差分通信最適化。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 2-2）
"""

from __future__ import annotations

import calendar
from datetime import UTC, date, datetime, time, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from zoneinfo import ZoneInfo

import feedparser
import httpx

from app.core.config import DEFAULT_SUMMARY_CHAR_LIMIT, get_settings
from app.core.logging import get_logger, mask_sensitive_text
from app.core.security import (
    SSRFValidationError,
    normalize_url,
    sanitize_html_text,
    validate_safe_url,
)
from app.models.rss_source import RssSource
from app.services.filter_service import main as run_primary_filter

# =============================================================================
# 設定値ブロック（日本時間タイムゾーン定義）
# =============================================================================

JST_ZONE = ZoneInfo("Asia/Tokyo")
logger = get_logger(__name__)


def get_jst_day_utc_bounds(target_date: date) -> tuple[datetime, datetime]:
    """日本時間（Asia/Tokyo）の対象日 0:00:00 から翌日 0:00:00 直前までのUTC日時範囲を返す（指示書 §6.1）。

    解説：
        例えば対象日が `2026-11-02` の場合：
        - 開始：2026-11-02 00:00:00 JST（＝ 2026-11-01 15:00:00 UTC）
        - 終了：2026-11-03 00:00:00 JST（＝ 2026-11-02 15:00:00 UTC、この時刻未満が対象）

    Args:
        target_date (date): 日本時間での取得対象日。

    Returns:
        tuple[datetime, datetime]: `(start_utc, end_utc)` のタプル。

    Side Effects:
        なし。

    Examples:
        >>> from datetime import date
        >>> s, e = get_jst_day_utc_bounds(date(2026, 11, 2))
        >>> (e - s).total_seconds()
        86400.0
    """
    start_jst = datetime.combine(target_date, time(0, 0, 0), tzinfo=JST_ZONE)
    end_jst = start_jst + timedelta(days=1)
    return start_jst.astimezone(UTC), end_jst.astimezone(UTC)


def format_datetime_jst(dt: datetime | None) -> str:
    """UTCまたは任意のタイムゾーン付き `datetime` を日本時間の表示文字列へ変換する（指示書 §19）。

    Args:
        dt (datetime | None): 変換対象の日時。

    Returns:
        str: `"YYYY/MM/DD HH:MM"` 形式の日本時間文字列。`None` の場合は `"日時不明"`。

    Side Effects:
        なし。

    Examples:
        >>> from datetime import datetime, timezone
        >>> format_datetime_jst(datetime(2026, 11, 1, 22, 0, tzinfo=timezone.utc))
        '2026/11/02 07:00'
    """
    if dt is None:
        return "日時不明"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(JST_ZONE).strftime("%Y/%m/%d %H:%M")


def _parse_entry_datetime(
    entry: dict[str, Any],
    struct_keys: tuple[str, ...],
    str_keys: tuple[str, ...],
) -> datetime | None:
    """feedparser の entry から公開日時または更新日時をUTC `datetime` として抽出する。

    Args:
        entry (dict[str, Any]): feedparser の記事エントリ辞書。
        struct_keys (tuple[str, ...]): `published_parsed` 等の `time.struct_time` キー候補。
        str_keys (tuple[str, ...]): `published` 等の生文字列キー候補。

    Returns:
        datetime | None: 解析できた場合はUTC `datetime`、不明な場合は `None`。

    Side Effects:
        なし。
    """
    for s_key in struct_keys:
        parsed_tuple = entry.get(s_key)
        if parsed_tuple is not None:
            try:
                ts = calendar.timegm(parsed_tuple)
                return datetime.fromtimestamp(ts, tz=UTC)
            except Exception:
                continue

    for str_key in str_keys:
        raw_val = entry.get(str_key)
        if raw_val and isinstance(raw_val, str):
            raw_clean = raw_val.strip()
            try:
                dt = parsedate_to_datetime(raw_clean)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                return dt.astimezone(UTC)
            except Exception:
                pass
            try:
                iso_str = raw_clean.replace("Z", "+00:00")
                dt = datetime.fromisoformat(iso_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                return dt.astimezone(UTC)
            except Exception:
                continue

    return None


def parse_rss_xml_content(
    xml_content: str | bytes,
    source_name: str,
    source_id: int | None = None,
    allow_store_summary: bool = True,
    summary_char_limit: int = DEFAULT_SUMMARY_CHAR_LIMIT,
    excluded_categories: list[str] | None = None,
) -> list[dict[str, Any]]:
    """RSS/Atom のXML文字列を解析し、現在公開されている全記事を正規化・一次フィルタする（指示書 §5.1）。

    解説：
        - 取得した全エントリを走査し、最初から15件に絞ることはしない。
        - 利用条件上保存が許可されている場合（`allow_store_summary=True`）のみ概要を保持し、
          `summary_char_limit`（初期値200文字）以内のプレーンテキストに無害化する。
        - 公開日時が取得できない記事は `is_pubdate_verified=False` とし、勝手に対象日内と断定しない。

    Args:
        xml_content (str | bytes): RSS/Atom XMLデータ。
        source_name (str): 配信元名。
        source_id (int | None): 情報源ID。
        allow_store_summary (bool): 概要保存の可否。
        summary_char_limit (int): 概要保存最大文字数。
        excluded_categories (list[str] | None): 除外カテゴリキー一覧。

    Returns:
        list[dict[str, Any]]: 解析・一次フィルタ済みの記事辞書リスト。

    Side Effects:
        なし。
    """
    parsed_feed = feedparser.parse(xml_content)
    entries = getattr(parsed_feed, "entries", []) or []
    items: list[dict[str, Any]] = []

    for entry in entries:
        raw_title = entry.get("title") or ""
        clean_title = sanitize_html_text(raw_title, max_length=500)
        raw_link = (entry.get("link") or "").strip()
        if not clean_title or not raw_link:
            continue

        try:
            safe_link = validate_safe_url(raw_link, resolve_dns=False)
            norm_link = normalize_url(safe_link)
        except SSRFValidationError:
            logger.warning("不安全な記事URLをスキップしました: source=%s", source_name)
            continue

        pub_dt = _parse_entry_datetime(
            entry,
            struct_keys=("published_parsed", "created_parsed", "updated_parsed"),
            str_keys=("published", "created", "dc_date", "updated"),
        )
        upd_dt = _parse_entry_datetime(
            entry,
            struct_keys=("updated_parsed",),
            str_keys=("updated",),
        )

        raw_guid = entry.get("id") or entry.get("guid") or None
        guid_str = sanitize_html_text(str(raw_guid), max_length=500) if raw_guid else None

        rss_cats: list[str] = []
        for tag in entry.get("tags") or []:
            term = tag.get("term") or tag.get("label")
            if term:
                clean_term = sanitize_html_text(str(term), max_length=80)
                if clean_term:
                    rss_cats.append(clean_term)

        summary_snippet: str | None = None
        if allow_store_summary and summary_char_limit > 0:
            raw_summary = entry.get("summary") or entry.get("description") or ""
            cleaned_sum = sanitize_html_text(raw_summary, max_length=summary_char_limit)
            summary_snippet = cleaned_sum if cleaned_sum else None

        article_dict: dict[str, Any] = {
            "source_id": source_id,
            "source_name": source_name,
            "guid": guid_str,
            "url": safe_link,
            "normalized_url": norm_link,
            "title": clean_title,
            "published_at": pub_dt,
            "updated_at_rss": upd_dt,
            "is_pubdate_verified": pub_dt is not None,
            "rss_categories": rss_cats,
            "summary_snippet": summary_snippet,
            "excluded_categories": excluded_categories,
        }
        filtered_dict = run_primary_filter(article_dict)
        items.append(filtered_dict)

    return items


def fetch_single_feed(
    source: RssSource,
    summary_char_limit: int = DEFAULT_SUMMARY_CHAR_LIMIT,
    excluded_categories: list[str] | None = None,
    http_client: httpx.Client | None = None,
    resolve_dns: bool = False,
) -> dict[str, Any]:
    """1つの `RssSource` からHTTP経由でRSSを取得・解析する。

    Args:
        source (RssSource): 取得対象のRSS情報源モデル。
        summary_char_limit (int): 概要最大文字数。
        excluded_categories (list[str] | None): 除外カテゴリ設定。
        http_client (httpx.Client | None): 外部注入するHTTPクライアント（モックテスト用）。
        resolve_dns (bool): 実通信時にDNS解決先のプライベートIP検証を行うか。

    Returns:
        dict[str, Any]: `success` (bool), `items` (list), `error` (str | None) を含む辞書。

    Side Effects:
        指定されたRSS URLへHTTP GETリクエストを送信する。
    """
    if not source.is_safe_to_fetch:
        return {
            "success": False,
            "items": [],
            "error": f"情報源 '{source.name}' は無効または利用条件未確認のため自動取得を停止しています。",
        }

    try:
        safe_url = validate_safe_url(source.url, resolve_dns=resolve_dns)
    except SSRFValidationError as exc:
        return {"success": False, "items": [], "error": str(exc)}

    settings = get_settings()
    headers = {"User-Agent": settings.rss_user_agent}
    close_client = False
    client = http_client
    if client is None:
        client = httpx.Client(
            timeout=float(settings.rss_fetch_timeout_seconds),
            follow_redirects=True,
            headers=headers,
        )
        close_client = True

    try:
        response = client.get(safe_url, headers=headers)
        response.raise_for_status()
        # リダイレクト後の最終URLも安全か検証する（リダイレクトSSRF対策）
        validate_safe_url(str(response.url), resolve_dns=False)

        items = parse_rss_xml_content(
            xml_content=response.text,
            source_name=source.name,
            source_id=source.id,
            allow_store_summary=source.allow_store_summary,
            summary_char_limit=summary_char_limit,
            excluded_categories=excluded_categories,
        )
        return {"success": True, "items": items, "error": None}
    except Exception as exc:
        safe_err = mask_sensitive_text(str(exc))
        logger.warning("RSS取得エラー (%s): %s", source.name, safe_err)
        return {"success": False, "items": [], "error": safe_err}
    finally:
        if close_client and client is not None:
            client.close()


def filter_items_by_target_date(
    items: list[dict[str, Any]],
    target_date: date | None,
) -> dict[str, Any]:
    """日本時間の対象日（0:00〜翌日0:00直前）で記事リストを絞り込み、公開範囲外を検知する（指示書 §6.1）。

    解説：
        1. `target_date` が `None` の場合は全件をそのまま返す。
        2. `is_pubdate_verified == False`（公開日時が不明）の記事は、
           対象日内の記事と断定せず除外し `unverified_pubdate_count` に計上する。
        3. 取得された記事群に1件も `target_date` 内の記事が無い場合、
           RSSの公開範囲外である旨の通知メッセージ（`out_of_range_notice`）を生成する。

    Args:
        items (list[dict[str, Any]]): 記事辞書リスト。
        target_date (date | None): 日本時間での対象日。

    Returns:
        dict[str, Any]: `matched_items`, `unverified_pubdate_count`, `out_of_range_notice` を含む辞書。

    Side Effects:
        なし。
    """
    if target_date is None:
        return {
            "matched_items": list(items),
            "unverified_pubdate_count": sum(
                1 for i in items if not i.get("is_pubdate_verified")
            ),
            "out_of_range_notice": None,
        }

    start_utc, end_utc = get_jst_day_utc_bounds(target_date)
    matched: list[dict[str, Any]] = []
    unverified_count = 0
    verified_dates_jst: list[date] = []

    for item in items:
        pub_dt: datetime | None = item.get("published_at")
        if not item.get("is_pubdate_verified") or pub_dt is None:
            unverified_count += 1
            continue
        if pub_dt.tzinfo is None:
            pub_dt = pub_dt.replace(tzinfo=UTC)
        verified_dates_jst.append(pub_dt.astimezone(JST_ZONE).date())
        if start_utc <= pub_dt < end_utc:
            matched.append(item)

    out_of_range_notice: str | None = None
    if not matched:
        if verified_dates_jst:
            min_d = min(verified_dates_jst).isoformat()
            max_d = max(verified_dates_jst).isoformat()
            out_of_range_notice = (
                f"指定された対象日（{target_date.isoformat()}）の記事は現在のRSS公開範囲外です"
                f"（現在RSSで公開されている日付範囲：{min_d} 〜 {max_d}）。"
                "RSSに掲載されなくなった過去の記事は取得できません。"
            )
        else:
            out_of_range_notice = (
                f"指定された対象日（{target_date.isoformat()}）に該当する公開日時確認済みの記事が"
                "RSSフィード内に存在しません。"
            )

    return {
        "matched_items": matched,
        "unverified_pubdate_count": unverified_count,
        "out_of_range_notice": out_of_range_notice,
    }


def validate_input(payload: dict[str, Any]) -> dict[str, Any]:
    """RSS収集入力パラメータを検証する。"""
    if not isinstance(payload, dict):
        raise TypeError("payload は dict である必要があります。")
    return dict(payload)


def execute_logic(validated: dict[str, Any]) -> dict[str, Any]:
    """複数RSS情報源から一括収集し、対象日フィルタリングを適用する。"""
    sources: list[RssSource] = list(validated.get("sources") or [])
    summary_limit = int(validated.get("summary_char_limit", DEFAULT_SUMMARY_CHAR_LIMIT))
    excluded_cats = validated.get("excluded_categories")
    target_date: date | None = validated.get("target_date")
    http_client: httpx.Client | None = validated.get("http_client")

    all_fetched: list[dict[str, Any]] = []
    source_results: list[dict[str, Any]] = []
    success_sources = 0
    failed_sources = 0

    for src in sources:
        res = fetch_single_feed(
            source=src,
            summary_char_limit=summary_limit,
            excluded_categories=excluded_cats,
            http_client=http_client,
        )
        source_results.append(
            {
                "source_id": src.id,
                "source_name": src.name,
                "success": res["success"],
                "fetched_count": len(res["items"]),
                "error": res["error"],
            }
        )
        if res["success"]:
            success_sources += 1
            all_fetched.extend(res["items"])
        else:
            failed_sources += 1

    date_filtered = filter_items_by_target_date(all_fetched, target_date)
    total_sources = len(sources)
    failure_rate = round(failed_sources / total_sources, 4) if total_sources > 0 else 0.0

    return {
        "all_fetched_items": all_fetched,
        "target_items": date_filtered["matched_items"],
        "unverified_pubdate_count": date_filtered["unverified_pubdate_count"],
        "out_of_range_notice": date_filtered["out_of_range_notice"],
        "source_results": source_results,
        "success_sources": success_sources,
        "failed_sources": failed_sources,
        "rss_failure_rate": failure_rate,
    }


def format_output(result: dict[str, Any]) -> dict[str, Any]:
    """収集結果辞書を整形して返す。"""
    return dict(result)


def main(payload: dict[str, Any]) -> dict[str, Any]:
    """RSS収集・対象日フィルタリングの一連の処理を実行する。"""
    validated = validate_input(payload)
    result = execute_logic(validated)
    return format_output(result)
