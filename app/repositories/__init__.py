"""Repository層（データベース永続化・トランザクション管理）パッケージ。

目的（Why）:
    SQLAlchemy セッション操作を Repository 層に閉じ込め、Controller 層や Service 層から
    直接SQLやORMクエリ詳細を分離する（指示書 §4.1）。
仕様（What）:
    RSS取得元・ニュース記事・実行履歴・ユーザー設定の各Repositoryクラスの公開。
前提条件・依存関係（Assumption / Dependencies）:
    sqlalchemy>=2.0, `app.models`
入出力（I/O）:
    DBセッションを受け取り、ORMモデルまたは統計辞書を返す。
注意点（Caution）:
    トランザクションの整合性を保つため、複数テーブル更新時は同一セッション内で処理すること。
今後の拡張ポイント（Future Work）:
    Phase 2 / Phase 3 で `metrics_repo.py`, `usage_repo.py`, `model_repo.py` を追加。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 2-1）
"""

from __future__ import annotations

REPO_MODULES: tuple[str, ...] = (
    "rss_source_repo",
    "article_repo",
    "run_repo",
    "setting_repo",
)
__all__: list[str] = list(REPO_MODULES)
