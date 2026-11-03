"""ニュース処理実行履歴（news_runs）、実行別記事一覧（run_articles）、AI分析結果（news_analyses）のRepository。

目的（Why）:
    1実行単位（`run_id`）の開始・完了状態、その実行で選定された最大15記事のスナップショット、
    および記事別のAI分析結果を永続化し、過去履歴の不変性と既存分析の再利用を支える（指示書 §8, §14, §18, §19）。
仕様（What）:
    `RunRepository` クラスによる実行履歴管理・選定記事保存・過去実行取得。
前提条件・依存関係（Assumption / Dependencies）:
    sqlalchemy>=2.0, `app.models.analysis`, `app.models.article`, `app.models.run`
入出力（I/O）:
    入力: SQLAlchemy `Session`、`run_id`、選定記事リスト
    出力: `NewsRun`, `RunArticle`, `NewsAnalysis` オブジェクト
注意点（Caution）:
    過去の実行結果が後日の更新で書き換わらないよう、`save_run_articles` では
    必ず選定時点のカテゴリ・重要度・要約を `RunArticle` のスナップショット列へ複製する。
今後の拡張ポイント（Future Work）:
    長期保存時の古いプレビュー実行レコード自動クリーンアップ。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 2-1）
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import mask_sensitive_text
from app.models.analysis import NewsAnalysis
from app.models.article import NewsArticle
from app.models.rss_source import utc_now
from app.models.run import (
    RUN_STATUS_AI_PENDING,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_RUNNING,
    NewsRun,
    RunArticle,
    generate_run_id,
)

# =============================================================================
# 設定値ブロック
# =============================================================================

DEFAULT_HISTORY_LIMIT: int = 50


class RunRepository:
    """`news_runs`, `run_articles`, `news_analyses` の永続化を担当するRepositoryクラス。"""

    def __init__(self, session: Session) -> None:
        """Repositoryを初期化する。"""
        self.session = session

    def create_run(
        self,
        run_type: str = "manual",
        selected_categories: list[str] | None = None,
        target_date: date | None = None,
        parent_run_id: str | None = None,
        reused_analysis: bool = False,
    ) -> NewsRun:
        """新しいニュース処理実行（`NewsRun`）を開始状態で作成する。

        Args:
            run_type (str): 実行方法（`initial`, `manual`, `scheduled`, `recategorize`, `retry`, `catchup`）。
            selected_categories (list[str] | None): 選択されたカテゴリ一覧。
            target_date (date | None): 取得対象日。
            parent_run_id (str | None): 再選別・再実行時の元 `run_id`。
            reused_analysis (bool): 既存AI分析を再利用した実行かどうか。

        Returns:
            NewsRun: 作成された `NewsRun` インスタンス。

        Side Effects:
            `news_runs` テーブルへ INSERT し commit する。
        """
        run_obj = NewsRun(
            run_id=generate_run_id(),
            parent_run_id=parent_run_id,
            run_type=run_type,
            target_date=target_date,
            selected_categories=list(selected_categories or []),
            started_at=utc_now(),
            status=RUN_STATUS_RUNNING,
            reused_analysis=reused_analysis,
        )
        self.session.add(run_obj)
        self.session.commit()
        self.session.refresh(run_obj)
        return run_obj

    def finalize_run(
        self,
        run_id: str,
        status: str = RUN_STATUS_COMPLETED,
        fetched_count: int = 0,
        deduped_count: int = 0,
        ai_classified_count: int = 0,
        ai_summarized_count: int = 0,
        displayed_count: int = 0,
        used_provider: str | None = None,
        used_model_id: str | None = None,
        reused_analysis: bool | None = None,
        error_message: str | None = None,
    ) -> NewsRun | None:
        """実行履歴の件数・使用モデル・最終状態を更新して終了する。"""
        run_obj = self.get_run(run_id)
        if run_obj is None:
            return None

        run_obj.finished_at = utc_now()
        run_obj.status = status
        run_obj.fetched_count = int(fetched_count)
        run_obj.deduped_count = int(deduped_count)
        run_obj.ai_classified_count = int(ai_classified_count)
        run_obj.ai_summarized_count = int(ai_summarized_count)
        run_obj.displayed_count = int(displayed_count)
        if used_provider is not None:
            run_obj.used_provider = used_provider
        if used_model_id is not None:
            run_obj.used_model_id = used_model_id
        if reused_analysis is not None:
            run_obj.reused_analysis = reused_analysis
        if error_message is not None:
            run_obj.error_message = mask_sensitive_text(error_message)

        self.session.commit()
        self.session.refresh(run_obj)
        return run_obj

    def get_run(self, run_id: str) -> NewsRun | None:
        """`run_id` で実行履歴を1件取得する。"""
        return self.session.get(NewsRun, run_id)

    def get_latest_displayable_run(self) -> NewsRun | None:
        """基本画面に表示可能な最新の実行（`completed` または `ai_pending`）を取得する。"""
        stmt = (
            select(NewsRun)
            .where(NewsRun.status.in_([RUN_STATUS_COMPLETED, RUN_STATUS_AI_PENDING]))
            .order_by(NewsRun.started_at.desc())
            .limit(1)
        )
        return self.session.scalar(stmt)

    def list_runs(self, limit: int = DEFAULT_HISTORY_LIMIT) -> list[NewsRun]:
        """実行履歴一覧を新しい順に取得する（指示書 §18）。"""
        stmt = select(NewsRun).order_by(NewsRun.started_at.desc()).limit(limit)
        return list(self.session.scalars(stmt).all())

    def list_pending_runs(self) -> list[NewsRun]:
        """AI処理保留（`ai_pending`）となっている実行一覧を取得する（指示書 §12.3, §20）。"""
        stmt = (
            select(NewsRun)
            .where(NewsRun.status == RUN_STATUS_AI_PENDING)
            .order_by(NewsRun.started_at.desc())
        )
        return list(self.session.scalars(stmt).all())

    def get_latest_analysis_for_article(self, article_id: int) -> NewsAnalysis | None:
        """指定記事に対する最新の完了済みAI分析結果を取得する（再利用判定用：指示書 §8）。"""
        stmt = (
            select(NewsAnalysis)
            .where(NewsAnalysis.article_id == article_id)
            .order_by(NewsAnalysis.analyzed_at.desc(), NewsAnalysis.id.desc())
            .limit(1)
        )
        return self.session.scalar(stmt)

    def save_analysis(
        self,
        article_id: int,
        run_id: str | None,
        provider: str,
        model_id: str,
        category: str,
        importance_score: int,
        summary_ja: str,
        why_important: str,
        summary_status: str = "completed",
        is_excluded: bool = False,
    ) -> NewsAnalysis:
        """記事のAI分析結果を保存し、記事本体のカテゴリ・重要度も同期する。"""
        analysis = NewsAnalysis(
            article_id=article_id,
            run_id=run_id,
            provider=provider,
            model_id=model_id,
            category=category,
            is_excluded=is_excluded,
            importance_score=importance_score,
            summary_ja=summary_ja,
            why_important=why_important,
            summary_status=summary_status,
            analyzed_at=utc_now(),
        )
        self.session.add(analysis)

        article = self.session.get(NewsArticle, article_id)
        if article is not None:
            article.primary_category = category
            article.importance_score = importance_score
            if is_excluded:
                article.is_excluded = True
                article.exclusion_reason = "ai_classified_excluded"

        self.session.commit()
        self.session.refresh(analysis)
        return analysis

    def save_run_articles(
        self,
        run_id: str,
        selected_items: list[dict[str, Any]],
    ) -> list[RunArticle]:
        """1実行で選定された記事（最大15件）を順位・スナップショット付きで保存する（指示書 §18, §19）。

        Args:
            run_id (str): 対象 `run_id`。
            selected_items (list[dict[str, Any]]): `article` (`NewsArticle`), `analysis` (`NewsAnalysis | None`),
                                                   `is_reused` (`bool`) 等を含む辞書リスト。

        Returns:
            list[RunArticle]: 保存された `RunArticle` リスト。

        Side Effects:
            既存の同一 `run_id` の `run_articles` を置換し commit する。
        """
        existing_rows = list(
            self.session.scalars(
                select(RunArticle).where(RunArticle.run_id == run_id)
            ).all()
        )
        for row in existing_rows:
            self.session.delete(row)
        self.session.flush()

        saved_rows: list[RunArticle] = []
        for idx, item in enumerate(selected_items[:15], start=1):
            article: NewsArticle = item["article"]
            analysis: NewsAnalysis | None = item.get("analysis")
            is_reused = bool(item.get("is_reused", False))

            snap_cat = analysis.category if analysis else article.primary_category
            snap_imp = analysis.importance_score if analysis else article.importance_score
            if analysis:
                snap_sum = analysis.summary_ja
                snap_why = analysis.why_important
            else:
                snap_sum = item.get("fallback_summary") or (
                    article.summary_snippet
                    if article.summary_snippet
                    else "（AI要約未実行：RSS概要なし）"
                )
                snap_why = item.get("fallback_why") or "（Python一次選別による重要ニュース候補）"

            run_art = RunArticle(
                run_id=run_id,
                article_id=article.id,
                analysis_id=analysis.id if analysis else None,
                display_rank=idx,
                snapshot_category=snap_cat,
                snapshot_importance=snap_imp,
                snapshot_summary=snap_sum,
                snapshot_why_important=snap_why,
                is_reused=is_reused,
                selected_at=utc_now(),
            )
            self.session.add(run_art)
            saved_rows.append(run_art)

        self.session.commit()
        return saved_rows

    def get_run_articles_with_details(
        self, run_id: str
    ) -> list[tuple[RunArticle, NewsArticle]]:
        """指定 `run_id` に紐づく選定記事を順位順で取得する。"""
        stmt = (
            select(RunArticle, NewsArticle)
            .join(NewsArticle, RunArticle.article_id == NewsArticle.id)
            .where(RunArticle.run_id == run_id)
            .order_by(RunArticle.display_rank.asc())
        )
        return list(self.session.execute(stmt).all())  # type: ignore[arg-type]


def validate_input(data: dict[str, Any]) -> dict[str, Any]:
    """入力辞書を検証する。"""
    if "session" not in data or not isinstance(data["session"], Session):
        raise ValueError("有効な SQLAlchemy Session が必要です。")
    return data


def execute_logic(validated: dict[str, Any]) -> list[NewsRun]:
    """実行履歴一覧を取得する。"""
    repo = RunRepository(validated["session"])
    return repo.list_runs(limit=int(validated.get("limit", DEFAULT_HISTORY_LIMIT)))


def format_output(runs: list[NewsRun]) -> list[dict[str, Any]]:
    """実行履歴リストを辞書リストへ整形する。"""
    return [
        {
            "run_id": r.run_id,
            "run_type": r.run_type,
            "status": r.status,
            "displayed_count": r.displayed_count,
            "reused_analysis": r.reused_analysis,
        }
        for r in runs
    ]


def main(session: Session) -> list[dict[str, Any]]:
    """実行履歴の検索と整形を実行する。"""
    validated = validate_input({"session": session})
    runs = execute_logic(validated)
    return format_output(runs)
