"""重要ニュース最大15件選定・多様性制御・並び替えサービス。

目的（Why）:
    選択カテゴリ内の候補記事から、重要度・関心関連度・鮮度・信頼性・情報源/カテゴリの多様性を
    総合評価して最大15記事を選定する（指示書 §7, §17.1）。
仕様（What）:
    - 除外記事（`is_excluded == True`）の確実な排除
    - 同一イベントクラスタ（`cluster_id`）の重複表示抑制（最良1件を代表選出）
    - 同一情報源・同一カテゴリへの過度な偏りを緩和する多様性ペナルティ付き貪欲選定
    - 最大15件（またはユーザー設定の `display_count` 1〜15件）への絞り込みと並び替え
前提条件・依存関係（Assumption / Dependencies）:
    `app.core.config`, `app.models.article`
入出力（I/O）:
    入力: 候補記事リスト、選択カテゴリ、最大件数（上限15）、並び替え順
    出力: 選定された記事リスト（最大15件）
注意点（Caution）:
    候補が15件未満の場合は無理に水増しせず、存在する記事だけを選定すること（指示書 §7）。
今後の拡張ポイント（Future Work）:
    ユーザーの既読傾向に基づくパーソナライズ重み調整。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 2-2）
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.core.config import DEFAULT_DISPLAY_COUNT, MAX_DISPLAY_COUNT, MIN_DISPLAY_COUNT

# =============================================================================
# 設定値ブロック（多様性制御パラメータ）
# =============================================================================

# 同一配信元・同一カテゴリが既に選出されている場合に課す多様性減点（偏り防止）
SOURCE_REPEAT_PENALTY: float = 4.0
CATEGORY_REPEAT_PENALTY: float = 2.5


def _get_attr_or_key(obj: Any, key: str, default: Any = None) -> Any:
    """ORMモデルまたは辞書の両方から同じインターフェースで属性値を取得する。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _freshness_bonus(published_at: datetime | None) -> float:
    """公開日時の新しさに応じた鮮度ボーナス（0.0〜10.0）を計算する。"""
    if published_at is None:
        return 0.0
    now = datetime.now(UTC)
    pub_utc = (
        published_at.replace(tzinfo=UTC)
        if published_at.tzinfo is None
        else published_at.astimezone(UTC)
    )
    hours_ago = max(0.0, (now - pub_utc).total_seconds() / 3600.0)
    if hours_ago <= 6:
        return 10.0
    if hours_ago <= 24:
        return 6.0
    if hours_ago <= 48:
        return 3.0
    return 0.0


def select_top_articles(
    candidates: list[Any],
    selected_categories: list[str] | None = None,
    max_count: int = DEFAULT_DISPLAY_COUNT,
    sort_order: str = "importance",
    include_unclassified_fallback: bool = True,
) -> list[Any]:
    """候補記事群から多様性を考慮して最大15件（または `max_count` 件）を選定する（指示書 §7）。

    解説：
        1. `is_excluded == True` の記事を除外する。
        2. 選択されたカテゴリ（OR条件）に一致する記事を抽出する。
        3. 同じ出来事を報じる別メディア記事（同一 `cluster_id`）が複数ある場合、
           最も重要度が高い代表1件のみを選定候補に残し、枠の重複消費を防ぐ。
        4. 1件ずつ選ぶ際、既に選ばれた配信元・カテゴリには少しだけ減点（多様性ペナルティ）を
           与えることで、特定の1サイトや1カテゴリばかりが15件を占領しないようにする。
        5. 最後にユーザー指定の並び順（`importance` 重要度順 または `latest` 最新順）で整列する。

    Args:
        candidates (list[Any]): `NewsArticle` または記事辞書のリスト。
        selected_categories (list[str] | None): ユーザーの選択カテゴリキー一覧。
        max_count (int): 表示上限件数（1〜15にクランプされる）。
        sort_order (str): `"importance"` または `"latest"`。
        include_unclassified_fallback (bool): 選択カテゴリで枠が埋まらない場合に `unclassified` を補충するか。

    Returns:
        list[Any]: 選定・整列済みの記事リスト（最大15件）。

    Side Effects:
        なし。

    Examples:
        >>> c1 = {"id": 1, "title": "A", "source_name": "S1", "primary_category": "ai_llm", "importance_score": 80, "is_excluded": False}
        >>> c2 = {"id": 2, "title": "B", "source_name": "S2", "primary_category": "economy_finance", "importance_score": 90, "is_excluded": False}
        >>> res = select_top_articles([c1, c2], ["ai_llm", "economy_finance"], max_count=15)
        >>> len(res)
        2
    """
    target_limit = max(MIN_DISPLAY_COUNT, min(int(max_count), MAX_DISPLAY_COUNT))
    cat_set = set(selected_categories) if selected_categories else set()

    non_excluded = [
        c for c in candidates if not bool(_get_attr_or_key(c, "is_excluded", False))
    ]

    if cat_set:
        primary_pool = [
            c
            for c in non_excluded
            if str(_get_attr_or_key(c, "primary_category", "unclassified")) in cat_set
        ]
        if include_unclassified_fallback and len(primary_pool) < target_limit:
            unclassified_pool = [
                c
                for c in non_excluded
                if str(_get_attr_or_key(c, "primary_category", "unclassified")) == "unclassified"
                and c not in primary_pool
            ]
            pool = primary_pool + unclassified_pool
        else:
            pool = primary_pool
    else:
        pool = non_excluded

    # 同一イベントクラスタ（cluster_id）ごとに最良1件へ集約する
    best_by_cluster: dict[str, Any] = {}
    for item in pool:
        cid = str(
            _get_attr_or_key(item, "cluster_id")
            or _get_attr_or_key(item, "normalized_url")
            or id(item)
        )
        imp = int(_get_attr_or_key(item, "importance_score", 50))
        existing = best_by_cluster.get(cid)
        if existing is None or imp > int(_get_attr_or_key(existing, "importance_score", 50)):
            best_by_cluster[cid] = item

    remaining = list(best_by_cluster.values())
    selected: list[Any] = []
    source_counts: dict[str, int] = {}
    category_counts: dict[str, int] = {}

    while remaining and len(selected) < target_limit:
        best_item = max(
            remaining,
            key=lambda item: (
                float(_get_attr_or_key(item, "importance_score", 50))
                + _freshness_bonus(_get_attr_or_key(item, "published_at"))
                - SOURCE_REPEAT_PENALTY
                * source_counts.get(str(_get_attr_or_key(item, "source_name", "")), 0)
                - CATEGORY_REPEAT_PENALTY
                * category_counts.get(
                    str(_get_attr_or_key(item, "primary_category", "unclassified")), 0
                )
            ),
        )
        selected.append(best_item)
        remaining.remove(best_item)

        src_name = str(_get_attr_or_key(best_item, "source_name", ""))
        cat_name = str(_get_attr_or_key(best_item, "primary_category", "unclassified"))
        source_counts[src_name] = source_counts.get(src_name, 0) + 1
        category_counts[cat_name] = category_counts.get(cat_name, 0) + 1

    return sort_articles(selected, sort_order=sort_order)


def sort_articles(articles: list[Any], sort_order: str = "importance") -> list[Any]:
    """選定された記事リストを重要度順または最新順で並び替える（指示書 §17.1）。

    Args:
        articles (list[Any]): 並び替え対象の記事リスト。
        sort_order (str): `"importance"`（重要度順）または `"latest"`（最新順）。

    Returns:
        list[Any]: 整列済みの新しい記事リスト。

    Side Effects:
        なし。
    """
    epoch_utc = datetime(1970, 1, 1, tzinfo=UTC)

    def _pub_ts(item: Any) -> float:
        dt = _get_attr_or_key(item, "published_at")
        if dt is None:
            return epoch_utc.timestamp()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.timestamp()

    if sort_order == "latest":
        return sorted(
            articles,
            key=lambda x: (_pub_ts(x), int(_get_attr_or_key(x, "importance_score", 50))),
            reverse=True,
        )

    return sorted(
        articles,
        key=lambda x: (int(_get_attr_or_key(x, "importance_score", 50)), _pub_ts(x)),
        reverse=True,
    )


def validate_input(payload: dict[str, Any]) -> dict[str, Any]:
    """入力辞書を検証する。"""
    if not isinstance(payload, dict) or "candidates" not in payload:
        raise ValueError("selector_service には candidates を含む辞書が必要です。")
    return dict(payload)


def execute_logic(validated: dict[str, Any]) -> list[Any]:
    """重要ニュース選定を実行する。"""
    return select_top_articles(
        candidates=list(validated["candidates"]),
        selected_categories=validated.get("selected_categories"),
        max_count=int(validated.get("max_count", DEFAULT_DISPLAY_COUNT)),
        sort_order=str(validated.get("sort_order", "importance")),
        include_unclassified_fallback=bool(
            validated.get("include_unclassified_fallback", True)
        ),
    )


def format_output(selected: list[Any]) -> list[Any]:
    """選定結果リストを返す。"""
    return list(selected)


def main(payload: dict[str, Any]) -> list[Any]:
    """記事選定の一連の処理を実行する。"""
    validated = validate_input(payload)
    selected = execute_logic(validated)
    return format_output(selected)
