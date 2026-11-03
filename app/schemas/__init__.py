"""Pydantic 入出力検証スキーマパッケージ。

目的（Why）:
    Controller 層および Service 層で受け渡すデータの型・範囲検証を共通化する。
仕様（What）:
    記事・設定・実行サマリー用スキーマの公開。
前提条件・依存関係（Assumption / Dependencies）:
    pydantic>=2.7
入出力（I/O）:
    各スキーマクラスの公開。
注意点（Caution）:
    ニュースカード用スキーマにはトークン量や料金フィールドを含めないこと（指示書 §17.2）。
今後の拡張ポイント（Future Work）:
    Phase 3 で LLM 共通出力検証スキーマ（`llm_schema.py`）を追加。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 2-1）
"""

from __future__ import annotations

SCHEMA_MODULES: tuple[str, ...] = (
    "article_schema",
    "llm_schema",
    "run_schema",
    "setting_schema",
)
__all__: list[str] = list(SCHEMA_MODULES)
