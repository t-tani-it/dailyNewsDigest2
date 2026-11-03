"""Python一次フィルタリングおよびルールベースカテゴリ分類・重要度算出サービス。

目的（Why）:
    最初から全記事をLLMへ送るのではなく、Pythonによる事前処理で芸能・エンタメ・PR記事を
    記事単位で除外し、タイトル（および必要に応じた概要）からカテゴリと初期重要度を判定する（指示書 §2, §7）。
仕様（What）:
    - 記事単位での除外判定（芸能・エンタメ・広告PR・宣伝記事）
    - 判断が曖昧な記事は根拠なく除外せず `"unclassified"`（分類不能）として保持
    - 7大カテゴリ（特にAI・IT・経済・金融を重視）への一次分類と重要度スコア（1〜100）算出
前提条件・依存関係（Assumption / Dependencies）:
    `app.core.config`
入出力（I/O）:
    入力: 記事タイトル、概要、RSSカテゴリ、ユーザー除外カテゴリ設定
    出力: `is_excluded`, `exclusion_reason`, `primary_category`, `importance_score` を含む辞書
注意点（Caution）:
    除外判定はWebサイト単位ではなく、必ず記事単位で実行すること（指示書 §2.2）。
今後の拡張ポイント（Future Work）:
    ユーザー定義のカスタム除外キーワード辞書の統合。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 2-2）
"""

from __future__ import annotations

from typing import Any

from app.core.config import (
    DEFAULT_EXCLUDED_CATEGORIES,
    NEWS_CATEGORIES,
    PRIMARY_EXCLUSION_PATTERNS,
)

# =============================================================================
# 設定値ブロック（カテゴリ分類用キーワード辞書と情報源信頼度ボーナス）
# =============================================================================

CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "ai_llm": (
        "生成ai",
        "llm",
        "大規模言語モデル",
        "chatgpt",
        "openai",
        "gemini",
        "claude",
        "anthropic",
        "aiエージェント",
        "機械学習",
        "ディープラーニング",
        "rag",
        "推論モデル",
        "copilot",
        "人工知能",
    ),
    "it_cloud": (
        "aws",
        "azure",
        "google cloud",
        "gcp",
        "クラウド",
        "kubernetes",
        "docker",
        "linux",
        "python",
        "typescript",
        "github",
        "ソフトウェア",
        "システム障害",
        "データセンター",
        "半導体",
        "api",
        "saas",
    ),
    "economy_finance": (
        "日本銀行",
        "日銀",
        "金利",
        "利上げ",
        "利下げ",
        "インフレ",
        "物価",
        "為替",
        "円安",
        "円高",
        "ドル円",
        "株価",
        "日経平均",
        "資産運用",
        "nisa",
        "frb",
        "gdp",
        "決算",
        "金融政策",
        "国債",
    ),
    "cybersecurity": (
        "脆弱性",
        "サイバー攻撃",
        "ランサムウェア",
        "不正アクセス",
        "情報漏えい",
        "情報漏洩",
        "フィッシング",
        "マルウェア",
        "ゼロデイ",
        "cve-",
        "セキュリティ",
        "ipa",
        "jvn",
        "ddos",
    ),
    "science_tech": (
        "量子コンピュータ",
        "宇宙",
        "jaxa",
        "nasa",
        "核融合",
        "再生医療",
        "ips細胞",
        "新素材",
        "バッテリー",
        "全固体電池",
        "ロボット",
        "自動運転",
        "科学",
    ),
    "domestic_policy": (
        "政府",
        "内閣",
        "国会",
        "法案",
        "税制",
        "デジタル庁",
        "総務省",
        "経産省",
        "金融庁",
        "少子化",
        "自治体",
        "政策",
        "規制改革",
    ),
    "international": (
        "米国",
        "アメリカ",
        "欧州",
        "eu",
        "中国",
        "ウクライナ",
        "中東",
        "国連",
        "g7",
        "g20",
        "首脳会談",
        "関税",
        "外交",
        "国際",
    ),
}

# 信頼できる一次情報・公的機関・専門メディアに対する信頼性加点（指示書 §7）
TRUSTED_SOURCE_BONUS: dict[str, int] = {
    "日本銀行": 12,
    "IPA": 12,
    "AWS": 10,
    "NHK": 10,
    "ITmedia": 8,
}


def detect_exclusion(
    title: str,
    rss_categories: list[str] | None = None,
    excluded_categories: list[str] | None = None,
) -> tuple[bool, str | None]:
    """記事タイトルおよびRSSカテゴリから除外対象（芸能・エンタメ・PR等）か判定する（指示書 §2.2）。

    解説：
        Webサイト単位ではなく「記事単位」で検査を行う。
        明確にPR表記や芸能・エンタメ語句が含まれる場合のみ除外（`True`）とし、
        判断が曖昧な記事は根拠なく除外しない（`False, None` を返す）。

    Args:
        title (str): 記事タイトル。
        rss_categories (list[str] | None): RSSフィード上のカテゴリ名リスト。
        excluded_categories (list[str] | None): ユーザーが有効にしている除外カテゴリキー一覧。

    Returns:
        tuple[bool, str | None]: `(除外対象ならTrue, 除外理由カテゴリキーまたはNone)`。

    Side Effects:
        なし。

    Examples:
        >>> detect_exclusion("【PR】最新ノートPCセール情報")
        (True, 'advertisement_pr')
        >>> detect_exclusion("日銀が金融政策決定会合を開催")
        (False, None)
    """
    active_exclusions = (
        set(excluded_categories)
        if excluded_categories is not None
        else set(DEFAULT_EXCLUDED_CATEGORIES)
    )
    if not active_exclusions:
        return False, None

    target_text = f"{title} {' '.join(rss_categories or [])}".lower()

    for exc_key, patterns in PRIMARY_EXCLUSION_PATTERNS.items():
        if exc_key not in active_exclusions:
            continue
        for pat in patterns:
            if pat.lower() in target_text:
                return True, exc_key

    return False, None


def classify_article_by_text(
    title: str,
    summary_snippet: str | None = None,
    rss_categories: list[str] | None = None,
    use_summary_if_needed: bool = True,
) -> tuple[str, int]:
    """タイトル（および必要時のみ概要）からカテゴリと一致スコアを判定する（指示書 §7, §7.1）。

    解説：
        1. まずタイトルとRSSカテゴリだけで7大カテゴリのキーワード一致数を数える。
        2. タイトルだけで分類できない（一致数0）かつ `use_summary_if_needed=True` の場合のみ、
           概要（`summary_snippet`）を追加して再判定する（指示書 §7.1）。
        3. それでもどのカテゴリにも当てはまらない曖昧な記事は、根拠なく除外せず
           `"unclassified"`（分類不能）として返す（指示書 §2.2）。

    Args:
        title (str): ニュースタイトル。
        summary_snippet (str | None): 許可された範囲の記事概要。
        rss_categories (list[str] | None): RSS上のカテゴリ配列。
        use_summary_if_needed (bool): タイトルで分類不能な場合に概要を使用するか。

    Returns:
        tuple[str, int]: `(カテゴリキー, マッチしたキーワード数)`。

    Side Effects:
        なし。

    Examples:
        >>> classify_article_by_text("OpenAIが新型LLMとAIエージェントを発表")[0]
        'ai_llm'
        >>> classify_article_by_text("地域の天気と季節の話題")[0]
        'unclassified'
    """
    title_and_tags = f"{title} {' '.join(rss_categories or [])}".lower()

    def _score_text(text_lower: str) -> dict[str, int]:
        scores: dict[str, int] = {}
        for cat_key, keywords in CATEGORY_KEYWORDS.items():
            hits = sum(1 for kw in keywords if kw.lower() in text_lower)
            if hits > 0:
                scores[cat_key] = hits
        return scores

    matched = _score_text(title_and_tags)

    # タイトルだけで分類が難しい場合のみ概要先頭部分を追加して判定する（指示書 §7.1）
    if not matched and use_summary_if_needed and summary_snippet:
        combined_text = f"{title_and_tags} {summary_snippet.lower()}"
        matched = _score_text(combined_text)

    if not matched:
        return "unclassified", 0

    # ヒット数 × カテゴリの重視重み（AI・IT・経済・金融を優先：指示書 §2.1）で最良カテゴリを選ぶ
    best_cat = max(
        matched.keys(),
        key=lambda c: (
            matched[c] * float(NEWS_CATEGORIES.get(c, {}).get("priority_weight", 1.0)),
            matched[c],
        ),
    )
    return best_cat, matched[best_cat]


def calculate_initial_importance(
    category: str,
    keyword_hits: int,
    source_name: str,
    is_Excluded: bool = False,
) -> int:
    """カテゴリの重視度・キーワード一致数・情報源信頼性から初期重要度（1〜100）を算出する。

    Args:
        category (str): 判定されたカテゴリキー。
        keyword_hits (int): キーワードヒット数。
        source_name (str): 配信元名。
        is_Excluded (bool): 除外記事の場合は最低スコアとする。

    Returns:
        int: 1〜100 の重要度スコア。

    Side Effects:
        なし。

    Examples:
        >>> calculate_initial_importance("ai_llm", 2, "ITmedia") > calculate_initial_importance("unclassified", 0, "その他")
        True
    """
    if is_Excluded:
        return 1

    base_score = 50.0
    weight = float(NEWS_CATEGORIES.get(category, {}).get("priority_weight", 1.0))
    base_score = base_score * weight
    base_score += min(20, keyword_hits * 6)

    for trusted_key, bonus in TRUSTED_SOURCE_BONUS.items():
        if trusted_key.lower() in source_name.lower():
            base_score += bonus
            break

    return max(1, min(100, int(round(base_score))))


def validate_input(article_data: dict[str, Any]) -> dict[str, Any]:
    """一次フィルタ対象の入力辞書を検証する。

    Args:
        article_data (dict[str, Any]): `title` を含む記事辞書。

    Returns:
        dict[str, Any]: 検証済み辞書。

    Raises:
        ValueError: `title` が空または存在しない場合。

    Side Effects:
        なし。

    Examples:
        >>> validate_input({"title": "日銀が金利据え置き"})["title"]
        '日銀が金利据え置き'
    """
    if not isinstance(article_data, dict) or not str(article_data.get("title") or "").strip():
        raise ValueError("一次フィルタには空でない title が必須です。")
    return dict(article_data)


def execute_logic(validated_data: dict[str, Any]) -> dict[str, Any]:
    """記事1件に対して除外判定・一次カテゴリ分類・初期重要度算出を実行する。

    Args:
        validated_data (dict[str, Any]): `validate_input` 通過後の辞書。

    Returns:
        dict[str, Any]: フィルタ結果フィールドを付与した記事辞書。

    Side Effects:
        なし。

    Examples:
        >>> res = execute_logic({"title": "AWSが生成AI基盤を強化", "source_name": "AWS"})
        >>> res["is_excluded"]
        False
        >>> res["primary_category"] in ("ai_llm", "it_cloud")
        True
    """
    title = str(validated_data["title"]).strip()
    summary = validated_data.get("summary_snippet")
    rss_cats = list(validated_data.get("rss_categories") or [])
    excluded_cats = validated_data.get("excluded_categories")
    source_name = str(validated_data.get("source_name") or "")

    is_excluded, reason = detect_exclusion(
        title=title,
        rss_categories=rss_cats,
        excluded_categories=excluded_cats,
    )
    category, hits = classify_article_by_text(
        title=title,
        summary_snippet=summary,
        rss_categories=rss_cats,
        use_summary_if_needed=True,
    )
    importance = calculate_initial_importance(
        category=category,
        keyword_hits=hits,
        source_name=source_name,
        is_Excluded=is_excluded,
    )

    output = dict(validated_data)
    output.update(
        {
            "is_excluded": is_excluded,
            "exclusion_reason": reason,
            "primary_category": category,
            "importance_score": importance,
        }
    )
    return output


def format_output(result: dict[str, Any]) -> dict[str, Any]:
    """一次フィルタ結果辞書を整形して返す。"""
    return dict(result)


def main(article_data: dict[str, Any]) -> dict[str, Any]:
    """記事1件の一次フィルタ処理（検証・実行・整形）を実行する。"""
    validated = validate_input(article_data)
    processed = execute_logic(validated)
    return format_output(processed)
