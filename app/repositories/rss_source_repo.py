"""RSS取得元（rss_sources）のRepositoryモジュール。

目的（Why）:
    RSS取得元の初期シード登録、一覧取得、利用条件確認済み（`verified`）ソースの抽出、
    および取得結果・エラー履歴の更新を永続化する（指示書 §5.3, §19）。
仕様（What）:
    `RssSourceRepository` クラスによるCRUD操作。
前提条件・依存関係（Assumption / Dependencies）:
    sqlalchemy>=2.0, `app.core.config`, `app.models.rss_source`
入出力（I/O）:
    入力: SQLAlchemy `Session` およびRSS取得元属性
    出力: `RssSource` オブジェクトまたはリスト
注意点（Caution）:
    `list_fetchable_sources` は必ず `is_enabled == True` かつ `terms_status == "verified"` のみを返すこと。
今後の拡張ポイント（Future Work）:
    連続失敗回数が閾値を超えた際の自動クールダウン制御。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 2-1）
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import INITIAL_RSS_SOURCES
from app.core.logging import mask_sensitive_text
from app.models.rss_source import TERMS_STATUS_VERIFIED, RssSource, utc_now

# =============================================================================
# 設定値ブロック
# =============================================================================

MAX_ERROR_HISTORY_ITEMS: int = 20


class RssSourceRepository:
    """`rss_sources` テーブルへの永続化を担当するRepositoryクラス。"""

    def __init__(self, session: Session) -> None:
        """Repositoryを初期化する。

        Args:
            session (Session): SQLAlchemy セッション。

        Side Effects:
            なし。
        """
        self.session = session

    def seed_initial_sources(self) -> list[RssSource]:
        """初期RSS取得元候補が未登録の場合に登録する。

        Returns:
            list[RssSource]: DB内の全RSS取得元リスト。

        Side Effects:
            未登録の初期RSS取得元を `rss_sources` に INSERT し commit する。

        Examples:
            >>> # セッション経由で初期候補5件を登録
        """
        existing_urls = set(
            self.session.scalars(select(RssSource.url)).all()
        )
        added = False
        for item in INITIAL_RSS_SOURCES:
            if item["url"] not in existing_urls:
                src = RssSource(
                    name=str(item["name"]),
                    url=str(item["url"]),
                    category_hint=str(item.get("category_hint", "unclassified")),
                    is_enabled=bool(item.get("is_enabled", True)),
                    terms_status=str(item.get("terms_status", "unverified")),
                    allow_store_summary=bool(item.get("allow_store_summary", False)),
                    allow_ai_summary=bool(item.get("allow_ai_summary", False)),
                    last_status="never",
                    error_history=[],
                )
                self.session.add(src)
                added = True
        if added:
            self.session.commit()
        return self.list_all()

    def list_all(self) -> list[RssSource]:
        """登録されているすべてのRSS取得元をID順で取得する。

        Returns:
            list[RssSource]: 全RSS取得元リスト。

        Side Effects:
            なし。
        """
        stmt = select(RssSource).order_by(RssSource.id.asc())
        return list(self.session.scalars(stmt).all())

    def list_fetchable_sources(self) -> list[RssSource]:
        """現在自動取得が許可されている情報源（有効かつ利用条件確認済み）のみを取得する（指示書 §5.3）。

        Returns:
            list[RssSource]: 自動収集可能な情報源リスト。

        Side Effects:
            なし。
        """
        stmt = (
            select(RssSource)
            .where(
                RssSource.is_enabled.is_(True),
                RssSource.terms_status == TERMS_STATUS_VERIFIED,
            )
            .order_by(RssSource.id.asc())
        )
        return list(self.session.scalars(stmt).all())

    def get_by_id(self, source_id: int) -> RssSource | None:
        """IDでRSS取得元を1件取得する。

        Args:
            source_id (int): 情報源ID。

        Returns:
            RssSource | None: 該当レコードまたは None。

        Side Effects:
            なし。
        """
        return self.session.get(RssSource, source_id)

    def create_or_update(
        self,
        name: str,
        url: str,
        category_hint: str = "unclassified",
        is_enabled: bool = True,
        terms_status: str = "unverified",
        allow_store_summary: bool = False,
        allow_ai_summary: bool = False,
    ) -> RssSource:
        """RSS取得元を新規登録またはURL一致で更新する。

        Args:
            name (str): 情報源名。
            url (str): RSS URL。
            category_hint (str): 既定カテゴリヒント。
            is_enabled (bool): 有効・無効フラグ。
            terms_status (str): 利用条件確認状態（`verified` / `unverified` / `restricted`）。
            allow_store_summary (bool): 概要保存の可否。
            allow_ai_summary (bool): AI要約の可否。

        Returns:
            RssSource: 登録または更新されたレコード。

        Side Effects:
            DBへの INSERT または UPDATE と commit を行う。
        """
        existing = self.session.scalar(select(RssSource).where(RssSource.url == url))
        if existing is None:
            existing = RssSource(
                name=name,
                url=url,
                category_hint=category_hint,
                is_enabled=is_enabled,
                terms_status=terms_status,
                allow_store_summary=allow_store_summary,
                allow_ai_summary=allow_ai_summary,
                error_history=[],
            )
            self.session.add(existing)
        else:
            existing.name = name
            existing.category_hint = category_hint
            existing.is_enabled = is_enabled
            existing.terms_status = terms_status
            existing.allow_store_summary = allow_store_summary
            existing.allow_ai_summary = allow_ai_summary
            existing.updated_at = utc_now()
        self.session.commit()
        self.session.refresh(existing)
        return existing

    def record_fetch_result(
        self,
        source_id: int,
        success: bool,
        error_message: str | None = None,
    ) -> RssSource | None:
        """RSS取得の成功・失敗結果とエラー履歴を記録する。

        Args:
            source_id (int): 対象情報源ID。
            success (bool): 取得成功時 True。
            error_message (str | None): エラー発生時のメッセージ。

        Returns:
            RssSource | None: 更新後のレコード。

        Side Effects:
            `last_fetched_at`, `last_status`, `error_history` を更新し commit する。
        """
        src = self.get_by_id(source_id)
        if src is None:
            return None

        now = utc_now()
        src.last_fetched_at = now
        src.updated_at = now
        if success:
            src.last_status = "ok"
        else:
            src.last_status = "error"
            history = list(src.error_history or [])
            history.append(
                {
                    "timestamp": now.isoformat(),
                    "error": mask_sensitive_text(error_message or "unknown error"),
                }
            )
            src.error_history = history[-MAX_ERROR_HISTORY_ITEMS:]
        self.session.commit()
        return src


def validate_input(data: dict[str, Any]) -> dict[str, Any]:
    """入力辞書を検証する。"""
    if "session" not in data or not isinstance(data["session"], Session):
        raise ValueError("有効な SQLAlchemy Session が必要です。")
    return data


def execute_logic(validated: dict[str, Any]) -> list[RssSource]:
    """初期シードを実行して有効なRSS取得元リストを返す。"""
    repo = RssSourceRepository(validated["session"])
    repo.seed_initial_sources()
    return repo.list_fetchable_sources()


def format_output(sources: list[RssSource]) -> list[dict[str, Any]]:
    """取得元リストを辞書リストへ整形する。"""
    return [
        {
            "id": s.id,
            "name": s.name,
            "url": s.url,
            "terms_status": s.terms_status,
            "is_safe_to_fetch": s.is_safe_to_fetch,
        }
        for s in sources
    ]


def main(session: Session) -> list[dict[str, Any]]:
    """初期RSS取得元をシードし、取得可能なソース一覧を返す。"""
    validated = validate_input({"session": session})
    sources = execute_logic(validated)
    return format_output(sources)
