"""Service層（ビジネスロジック）パッケージ。

目的（Why）:
    RSS収集、重複排除、一次フィルタ、重要ニュース選定、AI要約、無料モデル管理、
    トークン集計、参考料金計算のビジネスロジックを Controller から分離する（指示書 §4.1）。
仕様（What）:
    各サービスクラス・関数の公開。
前提条件・依存関係（Assumption / Dependencies）:
    `app.core`, `app.models`, `app.repositories`, `app.schemas`
入出力（I/O）:
    Controller やスケジューラから呼び出され、検証済みスキーマや辞書を返す。
注意点（Caution）:
    ニュース収集とLLM分析処理を完全に分離し、LLM利用不可時も収集・選定が動作すること（指示書 §14）。
今後の拡張ポイント（Future Work）:
    非同期バッチパイプラインの拡張。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 2-2）
"""

from __future__ import annotations

SERVICE_MODULES: tuple[str, ...] = (
    "filter_service",
    "dedup_service",
    "rss_service",
    "selector_service",
    "digest_orchestrator",
)
__all__: list[str] = list(SERVICE_MODULES)
