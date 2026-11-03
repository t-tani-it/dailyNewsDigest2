"""ニュース記事（news_articles）のRepositoryモジュール。

目的（Why）:
    収集したRSS記事の重複排除付き保存、既存記事の更新検知、対象日・カテゴリによる抽出、
    および既読・未読状態の更新を永続化する（指示書 §5.2, §6.1, §7, §17.2）。
仕様（What）:
    `ArticleRepository` クラスによる記事登録・更新・検索・既読管理。
前提条件・依存関係（Assumption / Dependencies）:
    sqlalchemy>=2.0, `app.models.article`
入出力（I/O）:
    入力: SQLAlchemy `Session`、正規化済み記事データ辞書
    出力: `NewsArticle` リストおよび新規・更新・重複件数サマリー
注意点（Caution）:
    対象日指定検索では `is_pubdate_verified == True` の記事のみを対象日内と判定すること（指示書 §6.1）。
今後の拡張ポイント（Future Work）:
    全文検索インデックスを用いたキーワード絞り込み。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 2-1）
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.article import NewsArticle, compute_sha256_hex
from app.models.rss_source import utc_now

# =============================================================================
# 設定値ブロック
# =============================================================================

DEFAULT_CANDIDATE_LIMIT: int = 200


class ArticleRepository:
    """`news_articles` テーブルへの永続化・検索を担当するRepositoryクラス。"""

    def __init__(self, session: Session) -> None:
        """Repositoryを初期化する。

        Args:
            session (Session): SQLAlchemy セッション。
        """
        self.session = session

    def get_by_id(self, article_id: int) -> NewsArticle | None:
        """記事IDで1件取得する。"""
        return self.session.get(NewsArticle, article_id)

    def find_by_normalized_url(self, normalized_url: str) -> NewsArticle | None:
        """正規化URLが一致する既存記事を検索する。"""
        stmt = select(NewsArticle).where(NewsArticle.normalized_url == normalized_url)
        return self.session.scalar(stmt)

    def find_by_guid(self, guid: str, source_name: str | None = None) -> NewsArticle | None:
        """RSS GUID（および配信元名）が一致する既存記事を検索する。"""
        stmt = select(NewsArticle).where(NewsArticle.guid == guid)
        if source_name:
            stmt = stmt.where(NewsArticle.source_name == source_name)
        return self.session.scalar(stmt)

    def upsert_articles(self, articles_data: list[dict[str, Any]]) -> dict[str, Any]:
        """重複排除・更新検知を行いながら記事リストをDBへ保存する（指示書 §5.2, §7）。

        解説：
            1. `normalized_url` または同一配信元の `guid` で既存記事を検索する。
            2. 未登録なら新規レコードとして追加する（`new_count += 1`）。
            3. 登録済みでも `content_hash`（タイトル＋概要のハッシュ）または `updated_at_rss` が
               変化していれば、既存記事の更新として検知し `is_updated_article = True` に更新する（`updated_count += 1`）。
            4. 完全に同一なら重複としてスキップする（`duplicate_count += 1`）。

        Args:
            articles_data (list[dict[str, Any]]): 正規化・一次フィルタ済みの記事辞書リスト。

        Returns:
            dict[str, Any]: `articles`（対象全記事モデル）, `new_or_updated_articles`（未処理対象）,
                            `new_count`, `updated_count`, `duplicate_count` を含む辞書。

        Side Effects:
            `news_articles` テーブルへの INSERT / UPDATE と commit を行う。
        """
        all_articles: list[NewsArticle] = []
        new_or_updated: list[NewsArticle] = []
        new_count = 0
        updated_count = 0
        duplicate_count = 0

        for item in articles_data:
            norm_url = str(item["normalized_url"])
            guid = item.get("guid")
            source_name = str(item["source_name"])
            title_str = str(item["title"])
            summary_str = item.get("summary_snippet") or ""
            title_hash = str(item.get("title_hash") or compute_sha256_hex(title_str))
            content_hash = str(
                item.get("content_hash") or compute_sha256_hex(f"{title_str}\n{summary_str}")
            )

            existing = self.find_by_normalized_url(norm_url)
            if existing is None and guid:
                existing = self.find_by_guid(str(guid), source_name=source_name)

            if existing is None:
                pub_at = item.get("published_at")
                article = NewsArticle(
                    source_id=item.get("source_id"),
                    source_name=source_name,
                    guid=str(guid) if guid else None,
                    url=str(item["url"]),
                    normalized_url=norm_url,
                    title=title_str,
                    title_hash=title_hash,
                    published_at=pub_at,
                    updated_at_rss=item.get("updated_at_rss"),
                    is_pubdate_verified=bool(item.get("is_pubdate_verified", pub_at is not None)),
                    rss_categories=list(item.get("rss_categories") or []),
                    summary_snippet=summary_str if summary_str else None,
                    content_hash=content_hash,
                    cluster_id=str(item.get("cluster_id") or title_hash[:16]),
                    is_updated_article=False,
                    is_read=False,
                    is_excluded=bool(item.get("is_excluded", False)),
                    exclusion_reason=item.get("exclusion_reason"),
                    primary_category=str(item.get("primary_category", "unclassified")),
                    importance_score=int(item.get("importance_score", 50)),
                )
                self.session.add(article)
                self.session.flush()
                all_articles.append(article)
                new_or_updated.append(article)
                new_count += 1
            else:
                new_updated_rss = item.get("updated_at_rss")
                has_content_change = existing.content_hash != content_hash
                has_rss_timestamp_change = (
                    new_updated_rss is not None
                    and existing.updated_at_rss != new_updated_rss
                )
                if has_content_change or has_rss_timestamp_change:
                    existing.title = title_str
                    existing.title_hash = title_hash
                    if summary_str:
                        existing.summary_snippet = summary_str
                    existing.content_hash = content_hash
                    existing.updated_at_rss = new_updated_rss or utc_now()
                    existing.is_updated_article = True
                    existing.is_excluded = bool(
                        item.get("is_excluded", existing.is_excluded)
                    )
                    existing.exclusion_reason = item.get(
                        "exclusion_reason", existing.exclusion_reason
                    )
                    if item.get("primary_category") and existing.primary_category == "unclassified":
                        existing.primary_category = str(item["primary_category"])
                    existing.updated_at = utc_now()
                    self.session.flush()
                    all_articles.append(existing)
                    new_or_updated.append(existing)
                    updated_count += 1
                else:
                    all_articles.append(existing)
                    duplicate_count += 1

        self.session.commit()
        return {
            "articles": all_articles,
            "new_or_updated_articles": new_or_updated,
            "new_count": new_count,
            "updated_count": updated_count,
            "duplicate_count": duplicate_count,
        }

    def list_candidate_articles(
        self,
        selected_categories: list[str] | None = None,
        start_utc: datetime | None = None,
        end_utc: datetime | None = None,
        include_unclassified: bool = True,
        limit: int = DEFAULT_CANDIDATE_LIMIT,
    ) -> list[NewsArticle]:
        """除外されていない候補記事を対象期間・選択カテゴリ（OR条件）で検索する。

        Args:
            selected_categories (list[str] | None): 選択カテゴリのリスト。
            start_utc (datetime | None): 対象期間開始日時（UTC）。
            end_utc (datetime | None): 対象期間終了日時（UTC）。
            include_unclassified (bool): 分類不能（`unclassified`）記事も候補に含めるか。
            limit (int): 最大取得件数。

        Returns:
            list[NewsArticle]: 条件に一致する記事モデルリスト。

        Side Effects:
            なし。
        """
        stmt = select(NewsArticle).where(NewsArticle.is_excluded.is_(False))

        if start_utc is not None:
            # 公開日時が不明な記事（is_pubdate_verified=False）は対象日内の記事と断定しない（指示書 §6.1）
            stmt = stmt.where(
                NewsArticle.is_pubdate_verified.is_(True),
                NewsArticle.published_at >= start_utc,
            )
        if end_utc is not None:
            stmt = stmt.where(
                NewsArticle.is_pubdate_verified.is_(True),
                NewsArticle.published_at < end_utc,
            )

        if selected_categories:
            cats = list(selected_categories)
            if include_unclassified and "unclassified" not in cats:
                cats.append("unclassified")
            stmt = stmt.where(NewsArticle.primary_category.in_(cats))

        stmt = stmt.order_by(
            NewsArticle.importance_score.desc(),
            NewsArticle.published_at.desc(),
            NewsArticle.id.desc(),
        ).limit(limit)
        return list(self.session.scalars(stmt).all())

    def set_read_status(self, article_id: int, is_read: bool) -> NewsArticle | None:
        """記事の既読・未読状態を更新する（指示書 §17.2）。

        Args:
            article_id (int): 対象記事ID。
            is_read (bool): 既読にする場合 True、未読に戻す場合 False。

        Returns:
            NewsArticle | None: 更新後の記事モデル（存在しない場合は None）。

        Side Effects:
            `is_read` カラムを UPDATE し commit する。
        """
        article = self.get_by_id(article_id)
        if article is None:
            return None
        article.is_read = bool(is_read)
        article.updated_at = utc_now()
        self.session.commit()
        self.session.refresh(article)
        return article

    def count_cluster_members(self, cluster_id: str | None) -> int:
        """同じ出来事（`cluster_id`）を報道している別メディア記事数を返す（指示書 §5.2）。"""
        if not cluster_id:
            return 1
        stmt = select(func.count(NewsArticle.id)).where(NewsArticle.cluster_id == cluster_id)
        count = self.session.scalar(stmt)
        return max(1, int(count or 1))


def validate_input(data: dict[str, Any]) -> dict[str, Any]:
    """入力辞書を検証する。"""
    if "session" not in data or not isinstance(data["session"], Session):
        raise ValueError("有効な SQLAlchemy Session が必要です。")
    return data


def execute_logic(validated: dict[str, Any]) -> list[NewsArticle]:
    """候補記事一覧を取得する。"""
    repo = ArticleRepository(validated["session"])
    return repo.list_candidate_articles(
        selected_categories=validated.get("selected_categories"),
        limit=int(validated.get("limit", DEFAULT_CANDIDATE_LIMIT)),
    )


def format_output(articles: list[NewsArticle]) -> list[dict[str, Any]]:
    """記事リストを辞書リストへ変換する。"""
    return [
        {
            "id": a.id,
            "title": a.title,
            "source_name": a.source_name,
            "primary_category": a.primary_category,
            "is_read": a.is_read,
        }
        for a in articles
    ]


def main(session: Session, selected_categories: list[str] | None = None) -> list[dict[str, Any]]:
    """候補記事の検索・整形を実行する。"""
    validated = validate_input(
        {"session": session, "selected_categories": selected_categories}
    )
    articles = execute_logic(validated)
    return format_output(articles)
