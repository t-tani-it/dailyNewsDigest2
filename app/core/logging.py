"""機密情報マスク機能付きロギング管理モジュール。

目的（Why）:
    APIキー（Gemini / OpenRouter）・DBパスワード・ローカル絶対パスなどの公開禁止情報が
    標準出力やログファイル・エラーメッセージへ漏えいすることを防止する（指示書 §11, §21）。
仕様（What）:
    - 正規表現による機密文字列（APIキー、Bearerトークン、DB認証情報、ユーザーパス）の自動マスク
    - 標準 `logging.Filter` を継承した `SecretMaskingFilter` の提供
    - アプリケーション共通ロガーの初期化と取得
前提条件・依存関係（Assumption / Dependencies）:
    Python標準ライブラリ（logging, re）、`app.core.config`
入出力（I/O）:
    入力: 任意のログ文字列・例外メッセージ
    出力: 機密箇所を `[REDACTED]` 等に置換した安全な文字列およびロガー
注意点（Caution）:
    新しい認証ヘッダーやトークン形式を追加する場合は `SENSITIVE_PATTERNS` に追記すること。
今後の拡張ポイント（Future Work）:
    JSON構造化ログ出力フォーマッタの追加。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 1-3）
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.core.config import get_settings

# =============================================================================
# 設定値ブロック（マスク対象の正規表現パターンとログフォーマット定義）
# =============================================================================

MASK_PLACEHOLDER: str = "[REDACTED]"
DEFAULT_LOG_FORMAT: str = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"

# 解説：上から順番に適用される。具体的なトークン形式から汎用的な key=value の順に並べることで、
#       取りこぼしなく機密文字列をマスクする。
SENSITIVE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Google / Gemini APIキー（例: AIzaSy...）
    (re.compile(r"AIza[0-9A-Za-z_\-]{16,}"), "[REDACTED_GEMINI_KEY]"),
    # OpenRouter / OpenAI 系APIキー（例: sk-or-v1-..., sk-...）
    (re.compile(r"sk-(?:or-v1-)?[0-9A-Za-z_\-]{12,}"), "[REDACTED_API_KEY]"),
    # GitHub トークン（例: ghp_..., github_pat_...）
    (re.compile(r"(?:ghp_[0-9A-Za-z]{16,}|github_pat_[0-9A-Za-z_]{16,})"), "[REDACTED_GH_TOKEN]"),
    # Authorization Bearer ヘッダー値
    (re.compile(r"(?i)(Bearer\s+)[0-9A-Za-z._\-]{8,}"), r"\1[REDACTED_TOKEN]"),
    # DB接続URL内のパスワード（例: postgresql+psycopg://user:secret@host/db）
    (
        re.compile(r"(?i)((?:postgresql(?:\+psycopg)?|postgres)://[^:\s/]+:)([^@\s]+)(@)"),
        r"\1[REDACTED_DB_PASS]\3",
    ),
    # 環境変数・クエリ・辞書風のキー指定（例: GEMINI_API_KEY=xxx, api_key: "xxx"）
    (
        re.compile(
            r"(?i)((?:gemini_api_key|openrouter_api_key|api_key|x-goog-api-key|password|secret|token)"
            r"\s*[:=]\s*['\"]?)([^'\"\s,&;}{]+)"
        ),
        r"\1[REDACTED]",
    ),
    # Windows のユーザーフォルダ配下の絶対パス（例: ドライブ名から始まるユーザーパス）
    (
        re.compile(r"(?i)[a-z]:\\Users\\[^\\\s'\"<>|]+"),
        r"[REDACTED_USER_PATH]",
    ),
)


def validate_input(raw_text: Any) -> str:
    """マスク処理対象の入力値を検証し、文字列へ変換する。

    Args:
        raw_text (Any): ログメッセージまたは任意のオブジェクト。

    Returns:
        str: マスク処理可能な文字列。None の場合は空文字列。

    Side Effects:
        なし。

    Examples:
        >>> validate_input(None)
        ''
        >>> validate_input(12345)
        '12345'
    """
    if raw_text is None:
        return ""
    if isinstance(raw_text, str):
        return raw_text
    return str(raw_text)


def execute_logic(text: str) -> str:
    """定義済みの正規表現パターンを順次適用し、機密情報をマスクする。

    Args:
        text (str): `validate_input` を通過した元文字列。

    Returns:
        str: APIキー・トークン・DBパスワード・ローカルパスがマスクされた文字列。

    Side Effects:
        なし。

    Examples:
        >>> execute_logic("GEMINI_API_KEY=dummy-value")
        'GEMINI_API_KEY=[REDACTED]'
    """
    if not text:
        return ""

    masked = text
    # 設定済みの動的APIキーが万が一正規表現外の形式でも確実にマスクする
    settings = get_settings()
    for dynamic_secret in (settings.gemini_api_key, settings.openrouter_api_key):
        secret_clean = dynamic_secret.strip()
        if len(secret_clean) >= 6 and secret_clean in masked:
            masked = masked.replace(secret_clean, MASK_PLACEHOLDER)

    for pattern, replacement in SENSITIVE_PATTERNS:
        masked = pattern.sub(replacement, masked)
    return masked


def format_output(masked_text: str) -> str:
    """マスク済みテキストの最終整形を行う。

    Args:
        masked_text (str): `execute_logic` によりマスクされた文字列。

    Returns:
        str: 出力用文字列。

    Side Effects:
        なし。

    Examples:
        >>> format_output("safe log")
        'safe log'
    """
    return masked_text


def mask_sensitive_text(raw_text: Any) -> str:
    """任意の入力から機密情報をマスクした文字列を返すユーティリティ関数。

    Args:
        raw_text (Any): マスク対象の文字列または例外オブジェクト等。

    Returns:
        str: 公開禁止情報を含まない安全な文字列。

    Side Effects:
        なし。

    Examples:
        >>> mask_sensitive_text("Bearer dummy-token-12345")
        'Bearer [REDACTED_TOKEN]'
    """
    validated = validate_input(raw_text)
    masked = execute_logic(validated)
    return format_output(masked)


class SecretMaskingFilter(logging.Filter):
    """ログレコード内のメッセージ・引数・例外情報から機密情報を除去するフィルタ。"""

    def filter(self, record: logging.LogRecord) -> bool:
        """ログ出力直前にレコード内容をマスクする。

        Args:
            record (logging.LogRecord): Python標準ロギングのレコード。

        Returns:
            bool: 常に True（レコード自体は破棄せず、マスクして通過させる）。

        Side Effects:
            `record.msg` および `record.args` をマスク済み文字列へ書き換える。

        Examples:
            >>> flt = SecretMaskingFilter()
            >>> rec = logging.LogRecord("test", logging.INFO, "", 1, "GEMINI_API_KEY=secret123", (), None)
            >>> flt.filter(rec)
            True
            >>> "[REDACTED]" in rec.getMessage()
            True
        """
        # なぜ args を展開して msg に統合するか：`logger.info("key=%s", api_key)` の形式で
        # 呼び出された場合でも、引数の中のAPIキーを確実にマスクするため。
        try:
            rendered_message = record.getMessage()
        except Exception:
            rendered_message = str(record.msg)

        record.msg = mask_sensitive_text(rendered_message)
        record.args = ()
        return True


def setup_logging(level_override: str | None = None) -> logging.Logger:
    """アプリケーションのルートロガーに `SecretMaskingFilter` を設定する。

    Args:
        level_override (str | None): ログレベルを上書きする場合の文字列（INFO, DEBUG等）。

    Returns:
        logging.Logger: 設定済みのアプリケーションロガー（`dailyNewsDigest2`）。

    Side Effects:
        標準ロギングハンドラおよびフィルタを設定する。

    Examples:
        >>> logger = setup_logging("INFO")
        >>> logger.name
        'dailyNewsDigest2'
    """
    settings = get_settings()
    level_name = (level_override or settings.log_level).upper()
    level = getattr(logging, level_name, logging.INFO)

    app_logger = logging.getLogger(settings.app_name)
    app_logger.setLevel(level)

    if not app_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(DEFAULT_LOG_FORMAT))
        handler.addFilter(SecretMaskingFilter())
        app_logger.addHandler(handler)
    else:
        for handler in app_logger.handlers:
            if not any(isinstance(f, SecretMaskingFilter) for f in handler.filters):
                handler.addFilter(SecretMaskingFilter())

    return app_logger


def get_logger(name: str) -> logging.Logger:
    """機密マスクフィルタが適用されたモジュール別ロガーを取得する。

    Args:
        name (str): モジュール名（通常は `__name__`）。

    Returns:
        logging.Logger: 子ロガーインスタンス。

    Side Effects:
        親ロガーが未初期化の場合は初期化する。

    Examples:
        >>> log = get_logger("rss")
        >>> log.name.endswith("rss")
        True
    """
    parent = setup_logging()
    child_logger = parent.getChild(name)
    if not any(isinstance(f, SecretMaskingFilter) for f in child_logger.filters):
        child_logger.addFilter(SecretMaskingFilter())
    return child_logger


def main(sample_text: str = "") -> str:
    """ロギング初期化と入力テキストのマスク処理を実行する。

    Args:
        sample_text (str): マスク確認用のテキスト。

    Returns:
        str: マスク後のテキスト。

    Side Effects:
        アプリケーションロガーを初期化する。

    Examples:
        >>> main("OPENROUTER_API_KEY=dummy-value")
        'OPENROUTER_API_KEY=[REDACTED]'
    """
    setup_logging()
    validated = validate_input(sample_text)
    masked = execute_logic(validated)
    return format_output(masked)
