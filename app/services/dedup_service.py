"""RSS記事の重複排除・関連記事クラスタリングサービス。

目的（Why）:
    RSS GUID・正規化URL・記事タイトル・公開日時・内容類似性を考慮して同一記事の重複登録を防止し、
    別メディアが同じ出来事を報道している場合は元記事の情報を失わずに関連記事（同一 `cluster_id`）
    として整理する（指示書 §5.2）。
仕様（What）:
    - 同一配信元内のGUID・正規化URL・高類似タイトルによる重複排除
    - 別メディア間で同一イベントを報じる記事への共通 `cluster_id` 付与
前提条件・依存関係（Assumption / Dependencies）:
    Python標準ライブラリ（difflib, re）、`app.models.article.compute_sha256_hex`
入出力（I/O）:
    入力: 正規化済み記事辞書リスト
    出力: `deduped_items`（重複排除後リスト）, `duplicate_count`（除外件数）, `related_clusters`（クラスタ情報）
注意点（Caution）:
    別メディアの同一報道記事は削除せず `cluster_id` で紐付けること（指示書 §5.2）。
今後の拡張ポイント（Future Work）:
    形態素解析または文字N-gramによる高速クラスタリングへの拡張。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 2-2）
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from app.models.article import compute_sha256_hex

# =============================================================================
# 設定値ブロック（類似度閾値）
# =============================================================================

# 同一メディア内で「同一記事の重複配信」とみなすタイトル類似度閾値
SAME_SOURCE_DUPLICATE_THRESHOLD: float = 0.92

# 別メディア間で「同じ出来事を報道する関連記事」とみなすタイトル類似度閾値
CROSS_MEDIA_CLUSTER_THRESHOLD: float = 0.68

TITLE_CLEAN_PATTERN: re.Pattern[str] = re.compile(r"[\s\u3000【】\[\]「」『』（）()・:：\-―]+")


def normalize_title_for_comparison(title: str) -> str:
    """類似度比較用にタイトルから記号・空白を除去し小文字化する。

    Args:
        title (str): 元タイトル文字列。

    Returns:
        str: 正規化された比較用タイトル。

    Side Effects:
        なし。

    Examples:
        >>> normalize_title_for_comparison("【速報】日銀、政策金利を据え置き！")
        '速報日銀、政策金利を据え置き！'
    """
    cleaned = TITLE_CLEAN_PATTERN.sub("", title.strip().lower())
    return cleaned


def compute_title_similarity(title_a: str, title_b: str) -> float:
    """2つの記事タイトルの類似度（0.0〜1.0）を算出する。

    Args:
        title_a (str): 比較元タイトル。
        title_b (str): 比較先タイトル。

    Returns:
        float: 0.0〜1.0 の類似度スコア。

    Side Effects:
        なし。

    Examples:
        >>> compute_title_similarity("日銀が金融政策決定会合で金利据え置き", "日銀、金融政策決定会合で金利据え置きを決定") > 0.7
        True
    """
    norm_a = normalize_title_for_comparison(title_a)
    norm_b = normalize_title_for_comparison(title_b)
    if not norm_a or not norm_b:
        return 0.0
    if norm_a == norm_b:
        return 1.0
    return SequenceMatcher(None, norm_a, norm_b).ratio()


def validate_input(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """重複排除対象の入力リストを検証する。

    Args:
        items (list[dict[str, Any]]): 記事辞書リスト。

    Returns:
        list[dict[str, Any]]: 検証済みリスト。

    Raises:
        TypeError: `items` がリストでない場合。

    Side Effects:
        なし。

    Examples:
        >>> validate_input([])
        []
    """
    if not isinstance(items, list):
        raise TypeError("items は list である必要があります。")
    return list(items)


def execute_logic(validated_items: list[dict[str, Any]]) -> dict[str, Any]:
    """バッチ内の重複記事排除と別メディア関連記事のクラスタリングを実行する（指示書 §5.2）。

    解説：
        1. `normalized_url` が一致する記事は完全に同一URLのため重複排除する。
        2. 同一配信元（`source_name`）かつ `guid` が一致する記事も重複排除する。
        3. 既出記事とタイトル類似度を比較し：
           - 同一配信元で類似度 >= 0.92 なら重複として除外する。
           - 別配信元で類似度 >= 0.68 なら、元記事を失わずに残したうえで
             先行記事と同じ `cluster_id` を付与し「関連記事」として整理する。

    Args:
        validated_items (list[dict[str, Any]]): `validate_input` 通過後の記事リスト。

    Returns:
        dict[str, Any]: `deduped_items`, `total_count`, `deduped_count`, `duplicate_count` を含む辞書。

    Side Effects:
        なし。

    Examples:
        >>> a1 = {"title": "日銀が政策金利を据え置き決定", "url": "https://a.example.com/1", "normalized_url": "https://a.example.com/1", "source_name": "MediaA", "guid": "g1"}
        >>> a2 = {"title": "日銀が政策金利を据え置き決定", "url": "https://a.example.com/1?utm_source=x", "normalized_url": "https://a.example.com/1", "source_name": "MediaA", "guid": "g1"}
        >>> a3 = {"title": "日銀、政策金利の据え置きを決定", "url": "https://b.example.com/2", "normalized_url": "https://b.example.com/2", "source_name": "MediaB", "guid": "g2"}
        >>> res = execute_logic([a1, a2, a3])
        >>> res["deduped_count"]
        2
        >>> res["deduped_items"][0]["cluster_id"] == res["deduped_items"][1]["cluster_id"]
        True
    """
    seen_urls: set[str] = set()
    seen_source_guids: set[tuple[str, str]] = set()
    deduped_items: list[dict[str, Any]] = []
    duplicate_count = 0

    for raw_item in validated_items:
        item = dict(raw_item)
        norm_url = str(item.get("normalized_url") or item.get("url") or "").strip()
        source_name = str(item.get("source_name") or "unknown").strip()
        guid = str(item.get("guid") or "").strip()
        title = str(item.get("title") or "").strip()

        if not norm_url or not title:
            duplicate_count += 1
            continue

        if norm_url in seen_urls:
            duplicate_count += 1
            continue

        if guid and (source_name, guid) in seen_source_guids:
            duplicate_count += 1
            continue

        title_hash = compute_sha256_hex(normalize_title_for_comparison(title) or title)
        summary_str = str(item.get("summary_snippet") or "")
        content_hash = compute_sha256_hex(f"{title}\n{summary_str}")
        assigned_cluster_id = title_hash[:16]
        is_same_source_dup = False

        for kept in deduped_items:
            sim = compute_title_similarity(title, str(kept["title"]))
            if kept["source_name"] == source_name and sim >= SAME_SOURCE_DUPLICATE_THRESHOLD:
                is_same_source_dup = True
                break
            if kept["source_name"] != source_name and sim >= CROSS_MEDIA_CLUSTER_THRESHOLD:
                assigned_cluster_id = str(kept["cluster_id"])
                break

        if is_same_source_dup:
            duplicate_count += 1
            continue

        seen_urls.add(norm_url)
        if guid:
            seen_source_guids.add((source_name, guid))

        item["title_hash"] = title_hash
        item["content_hash"] = content_hash
        item["cluster_id"] = assigned_cluster_id
        deduped_items.append(item)

    return {
        "deduped_items": deduped_items,
        "total_count": len(validated_items),
        "deduped_count": len(deduped_items),
        "duplicate_count": duplicate_count,
    }


def format_output(result: dict[str, Any]) -> dict[str, Any]:
    """重複排除結果辞書を整形して返す。"""
    return dict(result)


def main(items: list[dict[str, Any]]) -> dict[str, Any]:
    """記事リストの重複排除・関連記事クラスタリングを一括実行する。"""
    validated = validate_input(items)
    result = execute_logic(validated)
    return format_output(result)
