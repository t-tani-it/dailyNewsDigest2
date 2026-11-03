"""アプリケーション全体の設定値・環境変数管理モジュール。

目的（Why）:
    システム内の定数・環境変数・無料LLMポリシー・カテゴリ定義を1箇所に集約し、
    マジックナンバーの散在や環境差異による設定ミスを防止する。
仕様（What）:
    - Pydantic Settings を用いた `.env` および環境変数の型安全な読み込み
    - 取得対象カテゴリ・除外カテゴリ・初期RSS候補・比較対象有料3モデルの定義
    - 「有料LLMのAPI呼び出し禁止」ポリシーを定数として強制
前提条件・依存関係（Assumption / Dependencies）:
    pydantic>=2.7, pydantic-settings>=2.3, python-dotenv>=1.0
入出力（I/O）:
    入力: 環境変数または `.env` ファイル
    出力: `Settings` インスタンスおよび各種定数辞書
注意点（Caution）:
    - APIキーの初期値は必ず空文字列とし、コード内に実キーを記述しないこと。
    - `ALLOW_PAID_LLM_EXECUTION` はいかなる設定画面・環境変数からも True に変更できない。
今後の拡張ポイント（Future Work）:
    外部設定ストアまたは管理画面からの動的カテゴリ重み付け調整。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 1-3）
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# =============================================================================
# 設定値ブロック（アプリケーション全体で共有する定数・辞書）
# =============================================================================

# 最重要ポリシー: 有料モデルのAPI実行は常に禁止（設定画面や環境変数で変更不可）
ALLOW_PAID_LLM_EXECUTION: bool = False

# Phase 2 のRSS取得量実測期間（開発指示書の7日間を2日間に読み替え）
PHASE2_MEASUREMENT_DAYS: int = 2

# ニュース表示件数の範囲と初期値（指示書 §1, §17.1）
MIN_DISPLAY_COUNT: int = 1
MAX_DISPLAY_COUNT: int = 15
DEFAULT_DISPLAY_COUNT: int = 15

# RSS記事概要の使用文字数範囲と初期値（指示書 §7.1）
MIN_SUMMARY_CHAR_LIMIT: int = 0
MAX_SUMMARY_CHAR_LIMIT: int = 500
DEFAULT_SUMMARY_CHAR_LIMIT: int = 200

# 取得対象カテゴリ定義（指示書 §2.1, §6.2）
# 解説：キーはシステム内部識別子、値は画面表示用の日本語ラベルと優先重みを表す。
NEWS_CATEGORIES: dict[str, dict[str, Any]] = {
    "ai_llm": {
        "label": "生成AI・LLM・AIエージェント",
        "group_label": "生成AI・IT",
        "priority_weight": 1.3,  # AI・IT・経済・金融を特に重視（指示書 §2.1）
    },
    "it_cloud": {
        "label": "IT・ソフトウェア・クラウド",
        "group_label": "生成AI・IT",
        "priority_weight": 1.25,
    },
    "economy_finance": {
        "label": "経済・金融・金利・資産運用",
        "group_label": "経済・金融",
        "priority_weight": 1.25,
    },
    "domestic_policy": {
        "label": "国内ニュース・政策",
        "group_label": "国内・国際",
        "priority_weight": 1.0,
    },
    "international": {
        "label": "国際ニュース",
        "group_label": "国内・国際",
        "priority_weight": 1.0,
    },
    "science_tech": {
        "label": "科学・技術革新",
        "group_label": "科学・技術",
        "priority_weight": 1.1,
    },
    "cybersecurity": {
        "label": "サイバーセキュリティ",
        "group_label": "セキュリティ",
        "priority_weight": 1.15,
    },
    "unclassified": {
        "label": "分類不能",
        "group_label": "その他",
        "priority_weight": 0.7,  # 曖昧な記事を根拠なく除外せず保持するための区分
    },
}

# 初期状態で選択されるカテゴリ一覧
DEFAULT_SELECTED_CATEGORIES: list[str] = [
    "ai_llm",
    "it_cloud",
    "economy_finance",
    "domestic_policy",
    "international",
    "science_tech",
    "cybersecurity",
]

# 除外対象カテゴリ定義（指示書 §2.2）
EXCLUDED_CATEGORIES: dict[str, str] = {
    "celebrity_talent": "芸能人・タレント情報",
    "entertainment_media": "映画・ドラマ・音楽・アイドル等のエンタメ情報",
    "advertisement_pr": "広告・PR記事",
    "low_value_promo": "ニュースとしての重要性が低い宣伝記事",
}

DEFAULT_EXCLUDED_CATEGORIES: list[str] = list(EXCLUDED_CATEGORIES.keys())

# Python一次フィルタ用の除外判定キーワード（記事単位のタイトル・カテゴリ検査用）
# 解説：明確に広告・PRや芸能・エンタメと判別できる標識のみを定義し、
#       曖昧な記事は誤除外せず「分類不能（unclassified）」へ回す設計とする。
PRIMARY_EXCLUSION_PATTERNS: dict[str, tuple[str, ...]] = {
    "advertisement_pr": (
        "【PR】",
        "[PR]",
        "（PR）",
        "(PR)",
        "【広告】",
        "Sponsored",
        "スポンサード",
        "提供：",
    ),
    "low_value_promo": (
        "【セール】",
        "タイムセール祭り",
        "クーポン配布中",
        "キャンペーン開催中",
    ),
    "celebrity_talent": (
        "熱愛報道",
        "結婚を発表",
        "離婚を発表",
        "芸能界引退",
        "グラビア",
    ),
    "entertainment_media": (
        "主題歌に決定",
        "ドラマ視聴率",
        "興行収入ランキング",
        "アイドルグループ",
        "新曲MV公開",
    ),
}

# Google AI Studio (Gemini API) Free Tier 対応確認済みモデルの初期許可リスト（指示書 §10.2）
# 解説：モデル一覧APIに存在するだけでは無料枠利用可と判断せず、
#       この許可リストと照合して一致したモデルのみ `verified_free` として扱う。
GEMINI_FREE_TIER_ALLOWLIST: tuple[str, ...] = (
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-1.5-flash",
    "gemini-1.5-flash-8b",
    "models/gemini-2.0-flash",
    "models/gemini-2.0-flash-lite",
    "models/gemini-1.5-flash",
    "models/gemini-1.5-flash-8b",
)

# OpenRouter 無料モデルの識別ルール（指示書 §10.1）
OPENROUTER_FREE_MODEL_IDS: tuple[str, ...] = ("openrouter/free",)
OPENROUTER_FREE_SUFFIX: str = ":free"

# 参考料金計算用の比較対象有料3モデル定義（指示書 §16.1, §16.2）
# 注意：プログラム内に恒久的な固定価格を埋め込まないため、初期状態では
#       単価を None（未確認＝「算出不可」）とし、公式APIまたは確認日時付きスナップショットから取得する。
COMPARISON_PAID_MODELS: tuple[dict[str, Any], ...] = (
    {
        "model_id": "deepseek/deepseek-v4.1-flash",
        "model_name": "DeepSeek V4.1 Flash",
        "provider": "openrouter",
        "default_input_price_per_1m": None,
        "default_output_price_per_1m": None,
    },
    {
        "model_id": "openai/gpt-6-luna",
        "model_name": "GPT-6 Luna",
        "provider": "openrouter",
        "default_input_price_per_1m": None,
        "default_output_price_per_1m": None,
    },
    {
        "model_id": "gemini-3.8-flash",
        "model_name": "Gemini 3.8 Flash",
        "provider": "gemini",
        "default_input_price_per_1m": None,
        "default_output_price_per_1m": None,
    },
)

# 初期RSS取得元候補（指示書 §5.3）
# 注意：利用条件確認状態（terms_status）が "verified" でない情報源は自動収集を停止する。
INITIAL_RSS_SOURCES: tuple[dict[str, Any], ...] = (
    {
        "name": "日本銀行 新着情報",
        "url": "https://www.boj.or.jp/rss/whatsnew.xml",
        "category_hint": "economy_finance",
        "is_enabled": True,
        "terms_status": "verified",
        "allow_store_summary": True,
        "allow_ai_summary": True,
    },
    {
        "name": "IPA 重要なセキュリティ情報",
        "url": "https://www.ipa.go.jp/security/rss/alert.rdf",
        "category_hint": "cybersecurity",
        "is_enabled": True,
        "terms_status": "verified",
        "allow_store_summary": True,
        "allow_ai_summary": True,
    },
    {
        "name": "AWS What's New (日本語)",
        "url": "https://aws.amazon.com/jp/about-aws/whats-new/recent/feed/",
        "category_hint": "it_cloud",
        "is_enabled": True,
        "terms_status": "verified",
        "allow_store_summary": True,
        "allow_ai_summary": True,
    },
    {
        "name": "ITmedia NEWS",
        "url": "https://rss.itmedia.co.jp/rss/2.0/news_bursts.xml",
        "category_hint": "ai_llm",
        "is_enabled": True,
        "terms_status": "verified",
        "allow_store_summary": True,
        "allow_ai_summary": True,
    },
    {
        "name": "NHKニュース 主要ニュース",
        "url": "https://www.nhk.or.jp/rss/news/cat0.xml",
        "category_hint": "domestic_policy",
        "is_enabled": True,
        "terms_status": "verified",
        "allow_store_summary": True,
        "allow_ai_summary": True,
    },
)


class Settings(BaseSettings):
    """環境変数および `.env` から読み込むアプリケーション設定クラス。

    解説：
        Pydantic の `BaseSettings` を継承することで、環境変数の文字列を
        自動的に `int` や `bool` に型変換し、範囲外の値を起動時に検出できる。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- アプリケーション基本設定 ---
    app_name: str = Field(default="dailyNewsDigest2", description="アプリケーション名")
    app_env: str = Field(default="local", description="実行環境識別子（local / test）")
    debug: bool = Field(default=False, description="デバッグモード")
    host: str = Field(default="0.0.0.0", description="バインドホスト")
    port: int = Field(default=8000, ge=1, le=65535, description="バインドポート")
    log_level: str = Field(default="INFO", description="ログ出力レベル")

    # --- データベース設定 ---
    postgres_user: str = Field(default="digest", description="PostgreSQLユーザー")
    postgres_password: str = Field(
        default="change_me_local_only", description="PostgreSQLパスワード"
    )
    postgres_db: str = Field(default="digest", description="PostgreSQLデータベース名")
    postgres_host: str = Field(default="localhost", description="PostgreSQLホスト")
    postgres_port: int = Field(default=5432, ge=1, le=65535, description="PostgreSQLポート")
    database_url: str = Field(
        default="postgresql+psycopg://digest:change_me_local_only@localhost:5432/digest",
        description="SQLAlchemy接続URL",
    )

    # --- タイムゾーン・定期実行設定 ---
    app_timezone: str = Field(default="Asia/Tokyo", description="表示・スケジュール基準タイムゾーン")
    scheduler_hour: int = Field(default=7, ge=0, le=23, description="毎朝の自動更新時刻（時）")
    scheduler_minute: int = Field(default=0, ge=0, le=59, description="毎朝の自動更新時刻（分）")

    # --- Gemini API設定（Free Tierのみ） ---
    gemini_api_key: str = Field(default="", description="Google AI Studio APIキー")
    gemini_enabled: bool = Field(default=False, description="Gemini利用有効フラグ")
    gemini_free_tier_confirmed: bool = Field(
        default=False,
        description="Google AI Studio側で請求先未設定（Free Tier）であることを確認済みか",
    )

    # --- OpenRouter API設定（無料モデルのみ） ---
    openrouter_api_key: str = Field(default="", description="OpenRouter APIキー")
    openrouter_enabled: bool = Field(default=False, description="OpenRouter利用有効フラグ")
    openrouter_base_url: str = Field(
        default="https://openrouter.ai/api/v1", description="OpenRouter APIベースURL"
    )

    # --- LLM安全制御 ---
    llm_enabled: bool = Field(default=False, description="LLM機能全体のON/OFF")
    llm_auto_switch: bool = Field(default=True, description="無料モデル自動切替のON/OFF")
    llm_max_retries: int = Field(
        default=2, ge=0, le=5, description="LLM呼び出し・検証エラー時の最大再試行回数（無限リトライ禁止）"
    )
    llm_request_timeout_seconds: int = Field(
        default=60, ge=5, le=180, description="LLM APIリクエストタイムアウト秒数"
    )
    llm_daily_call_limit: int = Field(
        default=200, ge=1, le=5000, description="1日あたりのAPI呼び出し回数安全上限"
    )

    # --- RSS取得設定 ---
    rss_fetch_timeout_seconds: int = Field(
        default=15, ge=3, le=60, description="RSSフィード取得タイムアウト秒数"
    )
    rss_user_agent: str = Field(
        default="dailyNewsDigest2/1.0", description="RSS取得時のUser-Agentヘッダー"
    )
    rss_summary_char_limit: int = Field(
        default=DEFAULT_SUMMARY_CHAR_LIMIT,
        ge=MIN_SUMMARY_CHAR_LIMIT,
        le=MAX_SUMMARY_CHAR_LIMIT,
        description="LLM分類・要約時に使用するRSS概要の先頭文字数（0〜500文字）",
    )
    rss_allowed_schemes: str = Field(
        default="http,https", description="RSS取得を許可するURLスキーム（カンマ区切り）"
    )

    # --- 表示件数設定 ---
    default_display_count: int = Field(
        default=DEFAULT_DISPLAY_COUNT,
        ge=MIN_DISPLAY_COUNT,
        le=MAX_DISPLAY_COUNT,
        description="初期表示件数（1〜15件）",
    )
    max_display_count: int = Field(
        default=MAX_DISPLAY_COUNT,
        ge=MIN_DISPLAY_COUNT,
        le=MAX_DISPLAY_COUNT,
        description="最大表示件数（15件固定）",
    )

    @property
    def allow_paid_llm(self) -> bool:
        """有料LLMモデルの実行可否を返す（常に False）。

        Returns:
            bool: 常に False（有料APIは一切実行しない）。

        Side Effects:
            なし。

        Examples:
            >>> Settings().allow_paid_llm
            False
        """
        return ALLOW_PAID_LLM_EXECUTION

    @property
    def allowed_schemes_set(self) -> set[str]:
        """許可されたURLスキームの集合を小文字で返す。

        Returns:
            set[str]: `{"http", "https"}` などのスキーム集合。

        Side Effects:
            なし。

        Examples:
            >>> "https" in Settings().allowed_schemes_set
            True
        """
        return {
            scheme.strip().lower()
            for scheme in self.rss_allowed_schemes.split(",")
            if scheme.strip()
        }

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        """ログレベル文字列を大文字に正規化し、妥当性を検証する。

        Args:
            value (str): 環境変数から渡されたログレベル文字列。

        Returns:
            str: 正規化されたログレベル（DEBUG/INFO/WARNING/ERROR/CRITICAL）。

        Raises:
            ValueError: 未対応のログレベルが指定された場合。

        Side Effects:
            なし。

        Examples:
            >>> Settings.normalize_log_level("info")
            'INFO'
        """
        upper_val = value.strip().upper()
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if upper_val not in valid_levels:
            raise ValueError(f"未対応のログレベルです: {value}")
        return upper_val


def validate_input(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """設定上書き辞書の入力検証を行う。

    Args:
        overrides (dict[str, Any] | None): テスト等で一時的に上書きする設定辞書。

    Returns:
        dict[str, Any]: 検証済みの設定上書き辞書。

    Raises:
        TypeError: `overrides` が辞書でも None でもない場合。
        ValueError: 有料モデル有効化など禁止項目が含まれる場合。

    Side Effects:
        なし。

    Examples:
        >>> validate_input({"debug": True})
        {'debug': True}
    """
    if overrides is None:
        return {}
    if not isinstance(overrides, dict):
        raise TypeError("overrides は dict または None である必要があります。")
    # なぜ必要か：外部入力や設定変更から有料LLM実行が有効化される経路を完全に遮断するため
    if overrides.get("allow_paid_llm") is True:
        raise ValueError("有料LLMモデルの実行はポリシーにより禁止されています。")
    return dict(overrides)


def execute_logic(validated_overrides: dict[str, Any]) -> Settings:
    """検証済みパラメータから `Settings` インスタンスを生成する。

    Args:
        validated_overrides (dict[str, Any]): `validate_input` を通過した辞書。

    Returns:
        Settings: 型検証済みのアプリケーション設定オブジェクト。

    Side Effects:
        なし（環境変数および `.env` の読み取りのみ）。

    Examples:
        >>> s = execute_logic({"app_env": "test"})
        >>> s.app_env
        'test'
    """
    return Settings(**validated_overrides)


def format_output(settings_obj: Settings) -> dict[str, Any]:
    """APIキー等の機密情報を除外した安全な設定サマリー辞書を整形する。

    Args:
        settings_obj (Settings): アプリケーション設定オブジェクト。

    Returns:
        dict[str, Any]: 画面表示や診断ログに出力可能な設定情報（APIキー値は含まない）。

    Side Effects:
        なし。

    Examples:
        >>> summary = format_output(Settings())
        >>> "gemini_api_key" in summary
        False
        >>> summary["allow_paid_llm"]
        False
    """
    # なぜキー値ではなく bool に変換するのか：ブラウザやログへのAPIキー漏えいを防ぐため（指示書 §11, §21）
    return {
        "app_name": settings_obj.app_name,
        "app_env": settings_obj.app_env,
        "app_timezone": settings_obj.app_timezone,
        "scheduler_time": f"{settings_obj.scheduler_hour:02d}:{settings_obj.scheduler_minute:02d}",
        "llm_enabled": settings_obj.llm_enabled,
        "llm_auto_switch": settings_obj.llm_auto_switch,
        "gemini_key_configured": bool(settings_obj.gemini_api_key.strip()),
        "gemini_free_tier_confirmed": settings_obj.gemini_free_tier_confirmed,
        "openrouter_key_configured": bool(settings_obj.openrouter_api_key.strip()),
        "rss_summary_char_limit": settings_obj.rss_summary_char_limit,
        "default_display_count": settings_obj.default_display_count,
        "max_display_count": settings_obj.max_display_count,
        "allow_paid_llm": settings_obj.allow_paid_llm,
        "phase2_measurement_days": PHASE2_MEASUREMENT_DAYS,
    }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """シングルトンとしてキャッシュされた `Settings` インスタンスを取得する。

    Returns:
        Settings: アプリケーション全体で共有する設定インスタンス。

    Side Effects:
        初回呼び出し時に `.env` / 環境変数を読み込みキャッシュする。

    Examples:
        >>> settings = get_settings()
        >>> settings.max_display_count
        15
    """
    return execute_logic({})


def main(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """設定読み込み・検証・安全整形の一連の処理を実行する。

    Args:
        overrides (dict[str, Any] | None): 上書き設定辞書。

    Returns:
        dict[str, Any]: 機密情報をマスク・除外した設定サマリー辞書。

    Side Effects:
        なし。

    Examples:
        >>> result = main({"app_env": "test"})
        >>> result["app_env"]
        'test'
    """
    validated = validate_input(overrides)
    settings_obj = execute_logic(validated)
    return format_output(settings_obj)
