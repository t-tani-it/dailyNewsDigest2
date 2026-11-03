"""LLM Provider（Adapter）パッケージ初期化モジュール。

目的（Why）:
    Service層から利用する基底ProviderおよびGemini / OpenRouter各アダプタを公開する。
仕様（What）:
    `BaseLlmProvider`, `FreePolicyViolationError`, `GeminiProvider`, `OpenRouterProvider` のエクスポート。
前提条件・依存関係（Assumption / Dependencies）:
    `app.providers.base_provider`, `app.providers.gemini_provider`, `app.providers.openrouter_provider`
入出力（I/O）:
    各Providerクラス。
注意点（Caution）:
    Controller層から直接本パッケージを呼び出さず、必ずService層経由で利用すること（指示書 §4.1）。
今後の拡張ポイント（Future Work）:
    新規プロバイダー追加時のファクトリ関数登録。
変更履歴（Change Log）:
    2026-11-03 - 初版作成（Step 4-1）
"""

from __future__ import annotations

from app.providers.base_provider import (
    BaseLlmProvider,
    FreePolicyViolationError,
    ProviderHttpError,
)
from app.providers.gemini_provider import GeminiProvider
from app.providers.openrouter_provider import OpenRouterProvider

__all__ = [
    "BaseLlmProvider",
    "FreePolicyViolationError",
    "GeminiProvider",
    "OpenRouterProvider",
    "ProviderHttpError",
]
