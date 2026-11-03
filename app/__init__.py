"""dailyNewsDigest2 アプリケーションパッケージ。

目的（Why）:
    国内外のRSSニュース収集と無料LLMによる重要ニュース選定・要約を提供する
    MVC + Service + Repository + Provider 構成のルートパッケージを定義する。
仕様（What）:
    アプリケーションバージョン情報の公開。
前提条件・依存関係（Assumption / Dependencies）:
    Python 3.11 以上。
入出力（I/O）:
    `__version__` 定数を外部モジュールへ提供する。
注意点（Caution）:
    公開禁止情報（APIキー・ローカルパス等）を含めないこと。
今後の拡張ポイント（Future Work）:
    サブパッケージ初期化フックの追加。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 1-3）
"""

# --- 設定値ブロック ---
APP_VERSION: str = "3.0.0"  # 開発指示書 v3.0 に対応するアプリケーションバージョン
__version__: str = APP_VERSION
