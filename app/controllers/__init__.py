"""Controller層（HTTPリクエスト受付・入力検証・Service呼び出し）パッケージ。

目的（Why）:
    HTTPリクエストの受付・Pydantic検証・Service層呼び出し・Viewへのデータ受け渡しを担当し、
    Controller内にRSS解析やLLM通信ロジックを混入させない（指示書 §4.1）。
仕様（What）:
    画面表示ControllerおよびJSON API Controllerの公開。
前提条件・依存関係（Assumption / Dependencies）:
    fastapi>=0.115, `app.services`, `app.schemas`
入出力（I/O）:
    FastAPI `APIRouter` インスタンス群。
注意点（Caution）:
    管理操作（設定変更・RSS取得元変更）はローカルまたは同一Wi-Fi内のクライアントからのみ許可する（指示書 §21）。
今後の拡張ポイント（Future Work）:
    Phase 2 / Phase 3 の計測・モデル管理エンドポイント拡張。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 2-3）
"""

from __future__ import annotations

CONTROLLER_MODULES: tuple[str, ...] = (
    "api_news_controller",
    "api_setting_controller",
    "view_controller",
)
__all__: list[str] = list(CONTROLLER_MODULES)
