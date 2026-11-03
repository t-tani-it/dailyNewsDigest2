"""定期実行・停止後補完取得ジョブパッケージ。

目的（Why）:
    FastAPIのWebプロセスと定期実行プロセスを分離し、毎朝7:00（Asia/Tokyo）の自動更新と
    PC停止・スリープ後の未取得分補完処理を実行する（指示書 §20）。
仕様（What）:
    `scheduler_worker` の公開。
前提条件・依存関係（Assumption / Dependencies）:
    apscheduler>=3.10, `app.services.digest_orchestrator`
入出力（I/O）:
    ワーカー起動エントリポイントおよび単発実行関数。
注意点（Caution）:
    二重実行防止ロックを必ず経由すること。
今後の拡張ポイント（Future Work）:
    24時間ごとの無料モデルカタログ自動更新ジョブの統合。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 2-4）
"""

from __future__ import annotations

__all__: list[str] = ["scheduler_worker"]
