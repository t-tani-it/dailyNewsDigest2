"""dailyNewsDigest2 コア基盤パッケージ。

目的（Why）:
    設定管理・データベース接続・セキュリティ検証・ログマスク処理を集約し、
    全レイヤーから安全に参照できる共通基盤を提供する。
仕様（What）:
    config, database, security, logging の公開。
前提条件・依存関係（Assumption / Dependencies）:
    pydantic, pydantic-settings, sqlalchemy。
入出力（I/O）:
    各コアモジュールへの参照を提供する。
注意点（Caution）:
    ビジネスロジックは Service 層に配置し、本パッケージには含めないこと。
今後の拡張ポイント（Future Work）:
    監査ログ出力インターフェースの統合。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 1-3）
"""

# --- 設定値ブロック ---
CORE_MODULES: tuple[str, ...] = ("config", "database", "security", "logging")
__all__: list[str] = list(CORE_MODULES)
