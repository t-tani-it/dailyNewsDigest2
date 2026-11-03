"""ニュース収集・初回確認・選定・再選別・停止後補完取得を統括するオーケストレータサービス。

目的（Why）:
    初回起動時の確認フロー（はい/いいえ/キャンセル）、2回目以降の更新、カテゴリ変更時の再選別、
    およびPC停止・スリープ後の未取得分補完取得を1実行単位（`run_id`）で統合管理する（指示書 §6, §7, §8, §14, §20）。
仕様（What）:
    - `prepare_onboarding_preview`: DB確定保存を行わずに一時プレビューとカテゴリ別暫定件数を生成
    - `handle_onboarding_decision`: 「はい（DB保存・自動更新有効化）」「いいえ（タイトルのみ）」「キャンセル（破棄）」の分岐
    - `execute_digest_run`: 二重実行防止ロック付きのRSS収集・重複排除・最大15件選定・実行履歴保存
    - `recategorize_from_saved_articles`: RSS再取得を行わず保存済み記事・分析結果を再利用して再選別
    - `run_catchup_if_needed`: 前回成功時点以降の未取得分を補完取得
前提条件・依存関係（Assumption / Dependencies）:
    `app.repositories.*`, `app.services.*`, `app.schemas.*`
入出力（I/O）:
    入力: SQLAlchemy `Session`、実行パラメータ
    出力: 実行結果辞書、ニュースカードリスト、1実行単位サマリー
注意点（Caution）:
    初回確認で「はい」が押される前のデータは一時キャッシュにのみ保持し、DBへ確定保存しないこと（指示書 §6.3）。
今後の拡張ポイント（Future Work）:
    Phase 3 のAI分類・要約パイプライン呼び出しフックの接続。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 2-2 / Step 2-4）
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.core.config import (
    DEFAULT_DISPLAY_COUNT,
    DEFAULT_SELECTED_CATEGORIES,
    NEWS_CATEGORIES,
)
from app.core.logging import get_logger
from app.models.article import NewsArticle
from app.models.run import (
    RUN_STATUS_AI_PENDING,
    RUN_STATUS_COMPLETED,
    RUN_TYPE_CATCHUP,
    RUN_TYPE_INITIAL,
    RUN_TYPE_MANUAL,
    RUN_TYPE_RECATEGORIZE,
    RUN_TYPE_RETRY,
    NewsRun,
    RunArticle,
)
from app.providers.gemini_provider import GeminiProvider
from app.providers.openrouter_provider import OpenRouterProvider
from app.repositories.article_repo import ArticleRepository
from app.repositories.rss_source_repo import RssSourceRepository
from app.repositories.run_repo import RunRepository
from app.repositories.setting_repo import SettingRepository, _ensure_utc
from app.schemas.article_schema import (
    ONBOARDING_NOTICES,
    OnboardingPreviewResponseSchema,
)
from app.schemas.article_schema import (
    main as build_article_card_dict,
)
from app.schemas.run_schema import (
    main as build_run_summary_dict,
)
from app.services.classifier_service import classify_articles_with_llm
from app.services.dedup_service import main as run_dedup
from app.services.metrics_service import (
    record_daily_metric,
    record_daily_metric_from_collection,
)
from app.services.pricing_service import calculate_paid_model_estimates
from app.services.rss_service import (
    JST_ZONE,
    format_datetime_jst,
    get_jst_day_utc_bounds,
)
from app.services.rss_service import (
    main as collect_rss,
)
from app.services.selector_service import select_top_articles, sort_articles
from app.services.summarizer_service import summarize_selected_articles
from app.services.usage_service import aggregate_run_token_usage

# =============================================================================
# 設定値ブロック（初回確認前の一時プレビューキャッシュ）
# =============================================================================

# 初回確認前のデータはDBに確定保存せずメモリ上で一時保持する（指示書 §6.3）
_ONBOARDING_PREVIEW_CACHE: dict[str, dict[str, Any]] = {}
MAX_PREVIEW_CACHE_SIZE: int = 20

logger = get_logger(__name__)


def clear_preview_cache() -> None:
    """テストおよびキャンセル時用に一時プレビューキャッシュをクリアする。"""
    _ONBOARDING_PREVIEW_CACHE.clear()


def prepare_onboarding_preview(
    session: Session,
    target_date: date,
    selected_categories: list[str] | None = None,
    select_all: bool = False,
    http_client: httpx.Client | None = None,
) -> OnboardingPreviewResponseSchema:
    """初回起動時にRSSから対象日の記事を一時収集し、カテゴリ別暫定件数と注意事項を返す（指示書 §6）。

    解説：
        この段階では「取得していいですか？」の確認前であるため、
        収集した記事は `_ONBOARDING_PREVIEW_CACHE` に一時保持するだけで、
        `news_articles` テーブルへの確定保存は一切行わない（指示書 §6.3）。

    Args:
        session (Session): DBセッション（RSS取得元の読み取りのみに使用）。
        target_date (date): 日本時間での取得対象日。
        selected_categories (list[str] | None): 選択されたカテゴリリスト。
        select_all (bool): True の場合は全カテゴリを選択状態にする。
        http_client (httpx.Client | None): テスト用HTTPクライアント。

    Returns:
        OnboardingPreviewResponseSchema: 暫定件数・注意事項・プレビュートークンを含む応答。

    Side Effects:
        外部RSSへのHTTP取得と、メモリ一時キャッシュへの登録（DBへの記事保存は行わない）。
    """
    src_repo = RssSourceRepository(session)
    src_repo.seed_initial_sources()
    setting = SettingRepository(session).get_or_create()

    if select_all:
        cats = list(DEFAULT_SELECTED_CATEGORIES)
    else:
        cats = [
            c
            for c in (selected_categories or DEFAULT_SELECTED_CATEGORIES)
            if c in NEWS_CATEGORIES
        ]
        if not cats:
            cats = list(DEFAULT_SELECTED_CATEGORIES)

    fetchable_sources = src_repo.list_fetchable_sources()
    collected = collect_rss(
        {
            "sources": fetchable_sources,
            "summary_char_limit": setting.summary_char_limit,
            "excluded_categories": setting.excluded_categories,
            "target_date": target_date,
            "http_client": http_client,
        }
    )

    deduped = run_dedup(collected["target_items"])
    deduped_items: list[dict[str, Any]] = deduped["deduped_items"]

    # カテゴリ別の暫定件数を集計する（指示書 §6-6）
    category_counts: dict[str, int] = {c: 0 for c in NEWS_CATEGORIES}
    for item in deduped_items:
        if item.get("is_excluded"):
            continue
        cat_key = str(item.get("primary_category", "unclassified"))
        category_counts[cat_key] = category_counts.get(cat_key, 0) + 1

    preview_titles = [
        {
            "title": str(item["title"]),
            "source_name": str(item["source_name"]),
            "category": str(item.get("primary_category", "unclassified")),
            "category_label": str(
                NEWS_CATEGORIES.get(
                    str(item.get("primary_category", "unclassified")),
                    NEWS_CATEGORIES["unclassified"],
                )["label"]
            ),
            "published_at_jst_str": format_datetime_jst(item.get("published_at")),
            "url": str(item["url"]),
        }
        for item in deduped_items
        if not item.get("is_excluded")
        and (
            str(item.get("primary_category", "unclassified")) in cats
            or str(item.get("primary_category", "unclassified")) == "unclassified"
        )
    ]

    token = str(uuid.uuid4())
    if len(_ONBOARDING_PREVIEW_CACHE) >= MAX_PREVIEW_CACHE_SIZE:
        oldest_key = next(iter(_ONBOARDING_PREVIEW_CACHE))
        _ONBOARDING_PREVIEW_CACHE.pop(oldest_key, None)

    _ONBOARDING_PREVIEW_CACHE[token] = {
        "target_date": target_date,
        "selected_categories": cats,
        "total_fetched_count": len(collected["all_fetched_items"]),
        "deduped_items": deduped_items,
        "source_results": collected["source_results"],
        "rss_failure_rate": collected["rss_failure_rate"],
        "out_of_range_notice": collected["out_of_range_notice"],
        "preview_titles": preview_titles,
    }

    return OnboardingPreviewResponseSchema(
        preview_token=token,
        target_date=target_date,
        selected_categories=cats,
        total_fetched_count=len(collected["all_fetched_items"]),
        deduped_count=len(deduped_items),
        category_tentative_counts=category_counts,
        out_of_range_notice=collected["out_of_range_notice"],
        notices=list(ONBOARDING_NOTICES),
        preview_titles=preview_titles[:30],
    )


def handle_onboarding_decision(
    session: Session,
    preview_token: str,
    decision: str,
    selected_categories: list[str] | None = None,
) -> dict[str, Any]:
    """初回確認の「はい / いいえ / キャンセル」選択に応じた処理を実行する（指示書 §6.3）。

    解説：
        - `"yes"`（はい）：一時収集した記事をDBへ確定保存し、初回承認状態と毎朝の自動更新を
          有効化したうえで、ニュース選定（および有効なAI処理）を開始する。
        - `"no"`（いいえ）：AI処理を行わず、DBへの確定保存も行わず、記事タイトルのプレビューのみ返す。
        - `"cancel"`（キャンセル）：一時プレビューを破棄し、設定画面へ戻す。

    Args:
        session (Session): DBセッション。
        preview_token (str): `prepare_onboarding_preview` が発行したトークン。
        decision (str): `"yes"`, `"no"`, `"cancel"` のいずれか。
        selected_categories (list[str] | None): 選択カテゴリリスト。

    Returns:
        dict[str, Any]: 分岐処理結果の辞書。

    Side Effects:
        `decision == "yes"` の場合のみDBへの記事保存・設定更新・実行履歴保存を行う。
    """
    cached = _ONBOARDING_PREVIEW_CACHE.get(preview_token)

    if decision == "cancel":
        _ONBOARDING_PREVIEW_CACHE.pop(preview_token, None)
        return {
            "decision": "cancel",
            "status": "cancelled",
            "db_saved": False,
            "ai_executed": False,
            "redirect_to": "/settings",
            "preview_titles": [],
        }

    if decision == "no":
        titles = cached["preview_titles"] if cached else []
        return {
            "decision": "no",
            "status": "title_preview_only",
            "db_saved": False,
            "ai_executed": False,
            "redirect_to": None,
            "preview_titles": titles,
        }

    if decision != "yes":
        raise ValueError(f"未対応の decision です: {decision}")

    # 「はい」が選択された場合：DBへ確定保存し、初回承認と自動更新を有効化する
    cats = (
        list(selected_categories)
        if selected_categories
        else (cached["selected_categories"] if cached else list(DEFAULT_SELECTED_CATEGORIES))
    )
    setting_repo = SettingRepository(session)
    setting = setting_repo.approve_initial_onboarding(selected_categories=cats)

    if cached is not None:
        _ONBOARDING_PREVIEW_CACHE.pop(preview_token, None)
        src_repo = RssSourceRepository(session)
        for s_res in cached.get("source_results") or []:
            if s_res.get("source_id"):
                src_repo.record_fetch_result(
                    source_id=int(s_res["source_id"]),
                    success=bool(s_res["success"]),
                    error_message=s_res.get("error"),
                )

        art_repo = ArticleRepository(session)
        upsert_res = art_repo.upsert_articles(cached["deduped_items"])
        saved_articles: list[NewsArticle] = upsert_res["articles"]

        run_repo = RunRepository(session)
        run_obj = run_repo.create_run(
            run_type=RUN_TYPE_INITIAL,
            selected_categories=cats,
            target_date=cached.get("target_date"),
        )
        classify_res = classify_articles_with_llm(
            session=session,
            run_id=run_obj.run_id,
            articles=saved_articles,
        )
        selected_models = select_top_articles(
            candidates=saved_articles,
            selected_categories=cats,
            max_count=setting.display_count,
            sort_order=setting.sort_order,
        )
        summary_res = summarize_selected_articles(
            session=session,
            run_id=run_obj.run_id,
            selected_articles=selected_models,
        )
        run_items = summary_res["run_items"]
        run_repo.save_run_articles(run_obj.run_id, run_items)

        is_pending = bool(classify_res["ai_pending"] or summary_res["ai_pending"])
        final_status = RUN_STATUS_AI_PENDING if is_pending else RUN_STATUS_COMPLETED
        used_prov = summary_res["used_provider"] or classify_res["used_provider"]
        used_mod = summary_res["used_model_id"] or classify_res["used_model_id"]
        ai_ran = bool(
            classify_res["classified_count"] > 0 or summary_res["summarized_count"] > 0
        )

        run_repo.finalize_run(
            run_id=run_obj.run_id,
            status=final_status,
            fetched_count=int(cached["total_fetched_count"]),
            deduped_count=len(cached["deduped_items"]),
            ai_classified_count=int(classify_res["classified_count"]),
            ai_summarized_count=int(summary_res["summarized_count"]),
            displayed_count=len(selected_models),
            used_provider=used_prov,
            used_model_id=used_mod,
        )

        # Phase 2: 初回確認のRSS取得も実測データとして1日分を記録する。
        # 計測失敗でニュース保存自体を失敗させないよう、例外は警告に留める。
        try:
            record_daily_metric(
                session=session,
                metric_date=datetime.now(JST_ZONE).date(),
                total_fetched=int(cached["total_fetched_count"]),
                deduped_items=list(cached["deduped_items"]),
                rss_failure_rate=float(cached.get("rss_failure_rate") or 0.0),
            )
        except Exception as exc:
            session.rollback()
            logger.warning("Phase 2 計測値の保存に失敗しました（初回確認）: %s", exc)

        setting_repo.release_run_lock(run_obj.run_id, mark_success=True)
        return {
            "decision": "yes",
            "status": "approved_and_saved",
            "db_saved": True,
            "ai_executed": ai_ran,
            "run_id": run_obj.run_id,
            "redirect_to": "/",
            "preview_titles": [],
        }

    # キャッシュが存在しない場合は通常の初回収集を実行する
    run_res = execute_digest_run(
        session=session,
        run_type=RUN_TYPE_INITIAL,
        selected_categories=cats,
    )
    return {
        "decision": "yes",
        "status": "approved_and_saved",
        "db_saved": True,
        "ai_executed": False,
        "run_id": run_res.get("run_id"),
        "redirect_to": "/",
        "preview_titles": [],
    }


def execute_digest_run(
    session: Session,
    run_type: str = RUN_TYPE_MANUAL,
    target_date: date | None = None,
    selected_categories: list[str] | None = None,
    http_client: httpx.Client | None = None,
    gemini_provider: GeminiProvider | None = None,
    openrouter_provider: OpenRouterProvider | None = None,
) -> dict[str, Any]:
    """2回目以降のニュース収集・重複排除・一次フィルタ・最大15件選定を実行する（指示書 §7, §14, §20）。

    Args:
        session (Session): DBセッション。
        run_type (str): `"manual"`, `"scheduled"`, `"catchup"`, `"initial"` 等。
        target_date (date | None): 特定日指定がある場合の日本時間日付。
        selected_categories (list[str] | None): カテゴリ指定（省略時はユーザー設定を使用）。
        http_client (httpx.Client | None): テスト用HTTPクライアント。

    Returns:
        dict[str, Any]: `run_id`, `status`, `displayed_count` 等を含む実行結果辞書。

    Side Effects:
        RSS取得、DBへの記事保存、`news_runs` / `run_articles` の保存を行う。
    """
    setting_repo = SettingRepository(session)
    setting = setting_repo.get_or_create()
    cats = list(selected_categories or setting.selected_categories or DEFAULT_SELECTED_CATEGORIES)

    run_repo = RunRepository(session)
    run_obj = run_repo.create_run(
        run_type=run_type,
        selected_categories=cats,
        target_date=target_date,
    )

    if not setting_repo.acquire_run_lock(run_obj.run_id):
        run_repo.finalize_run(
            run_id=run_obj.run_id,
            status="failed",
            error_message="別のニュース更新処理が実行中のため二重実行を防止しました。",
        )
        return {
            "run_id": run_obj.run_id,
            "status": "locked",
            "displayed_count": 0,
            "error": "別のニュース更新処理が実行中です。",
        }

    try:
        src_repo = RssSourceRepository(session)
        src_repo.seed_initial_sources()
        sources = src_repo.list_fetchable_sources()

        collected = collect_rss(
            {
                "sources": sources,
                "summary_char_limit": setting.summary_char_limit,
                "excluded_categories": setting.excluded_categories,
                "target_date": target_date,
                "http_client": http_client,
            }
        )
        for s_res in collected["source_results"]:
            if s_res.get("source_id"):
                src_repo.record_fetch_result(
                    source_id=int(s_res["source_id"]),
                    success=bool(s_res["success"]),
                    error_message=s_res.get("error"),
                )

        deduped = run_dedup(collected["target_items"])
        deduped_items = deduped["deduped_items"]

        art_repo = ArticleRepository(session)
        upsert_res = art_repo.upsert_articles(deduped_items)

        classify_res = classify_articles_with_llm(
            session=session,
            run_id=run_obj.run_id,
            articles=upsert_res["new_or_updated_articles"],
            gemini_provider=gemini_provider,
            openrouter_provider=openrouter_provider,
        )

        start_utc: datetime | None = None
        end_utc: datetime | None = None
        if target_date is not None:
            start_utc, end_utc = get_jst_day_utc_bounds(target_date)

        candidates = art_repo.list_candidate_articles(
            selected_categories=cats,
            start_utc=start_utc,
            end_utc=end_utc,
            include_unclassified=True,
        )
        if not candidates and upsert_res["articles"]:
            candidates = [a for a in upsert_res["articles"] if not a.is_excluded]

        selected_articles = select_top_articles(
            candidates=candidates,
            selected_categories=cats,
            max_count=setting.display_count,
            sort_order=setting.sort_order,
        )

        summary_res = summarize_selected_articles(
            session=session,
            run_id=run_obj.run_id,
            selected_articles=selected_articles,
            gemini_provider=gemini_provider,
            openrouter_provider=openrouter_provider,
        )
        run_items = summary_res["run_items"]

        is_pending = bool(classify_res["ai_pending"] or summary_res["ai_pending"])
        final_status = RUN_STATUS_AI_PENDING if is_pending else RUN_STATUS_COMPLETED
        used_prov = summary_res["used_provider"] or classify_res["used_provider"]
        used_mod = summary_res["used_model_id"] or classify_res["used_model_id"]
        all_reused = bool(
            summary_res["reused_count"] > 0
            and summary_res["summarized_count"] == 0
            and classify_res["classified_count"] == 0
        )

        run_repo.save_run_articles(run_obj.run_id, run_items)
        final_run = run_repo.finalize_run(
            run_id=run_obj.run_id,
            status=final_status,
            fetched_count=len(collected["all_fetched_items"]),
            deduped_count=len(deduped_items),
            ai_classified_count=int(classify_res["classified_count"]),
            ai_summarized_count=int(summary_res["summarized_count"]),
            displayed_count=len(selected_articles),
            used_provider=used_prov,
            used_model_id=used_mod,
            reused_analysis=all_reused if all_reused else None,
        )

        # Phase 2: RSS取得量を1日1レコードで記録する（同日再実行時は最新値で上書き）。
        # 計測は補助機能のため、失敗しても本実行の成功状態は維持する。
        try:
            record_daily_metric_from_collection(
                session=session,
                collected=collected,
                deduped_items=deduped_items,
                metric_date=datetime.now(JST_ZONE).date(),
            )
        except Exception as exc:
            session.rollback()
            logger.warning("Phase 2 計測値の保存に失敗しました: %s", exc)

        setting_repo.release_run_lock(run_obj.run_id, mark_success=True)
        return {
            "run_id": run_obj.run_id,
            "status": final_run.status if final_run else RUN_STATUS_COMPLETED,
            "fetched_count": len(collected["all_fetched_items"]),
            "deduped_count": len(deduped_items),
            "new_count": upsert_res["new_count"],
            "updated_count": upsert_res["updated_count"],
            "displayed_count": len(selected_articles),
            "out_of_range_notice": collected["out_of_range_notice"],
        }
    except Exception as exc:
        setting_repo.release_run_lock(run_obj.run_id, mark_success=False)
        run_repo.finalize_run(
            run_id=run_obj.run_id,
            status="failed",
            error_message=str(exc),
        )
        raise


def recategorize_from_saved_articles(
    session: Session,
    selected_categories: list[str],
    display_count: int | None = None,
    sort_order: str | None = None,
    gemini_provider: GeminiProvider | None = None,
    openrouter_provider: OpenRouterProvider | None = None,
) -> dict[str, Any]:
    """カテゴリ変更時にRSSを再取得せず保存済み記事・既存分析を再利用して再選別する（指示書 §8, §18）。

    解説：
        - RSSの再取得は一切行わない（`fetched_count = 0`）。
        - 保存済みの分類・要約結果を優先再利用する。
        - 新たなLLM呼び出しを行わない場合は新規トークン消費を 0 とし、過去の消費量を再計上しない。
        - 未要約の選出記事があり `llm_enabled=True` の場合のみ、その不足分だけを要約する。

    Args:
        session (Session): DBセッション。
        selected_categories (list[str]): 新しく選択されたカテゴリ一覧。
        display_count (int | None): 表示件数（1〜15）。
        sort_order (str | None): `"importance"` または `"latest"`。
        gemini_provider (GeminiProvider | None): テスト用 Gemini アダプタ。
        openrouter_provider (OpenRouterProvider | None): テスト用 OpenRouter アダプタ。

    Returns:
        dict[str, Any]: 新しい `run_id` と再選別結果を含む辞書。

    Side Effects:
        ユーザー設定を更新し、元の `run_id` を `parent_run_id` に持つ新しい `NewsRun` を保存する。
    """
    setting_repo = SettingRepository(session)
    updates: dict[str, Any] = {"selected_categories": list(selected_categories)}
    if display_count is not None:
        updates["display_count"] = int(display_count)
    if sort_order is not None:
        updates["sort_order"] = str(sort_order)
    setting = setting_repo.update_settings(updates)

    run_repo = RunRepository(session)
    prev_run = run_repo.get_latest_displayable_run()
    parent_id = prev_run.run_id if prev_run else None

    new_run = run_repo.create_run(
        run_type=RUN_TYPE_RECATEGORIZE,
        selected_categories=setting.selected_categories,
        parent_run_id=parent_id,
        reused_analysis=True,
    )

    art_repo = ArticleRepository(session)
    candidates = art_repo.list_candidate_articles(
        selected_categories=setting.selected_categories,
        include_unclassified=True,
    )
    selected_articles = select_top_articles(
        candidates=candidates,
        selected_categories=setting.selected_categories,
        max_count=setting.display_count,
        sort_order=setting.sort_order,
    )

    summary_res = summarize_selected_articles(
        session=session,
        run_id=new_run.run_id,
        selected_articles=selected_articles,
        gemini_provider=gemini_provider,
        openrouter_provider=openrouter_provider,
    )
    run_items = summary_res["run_items"]
    for item in run_items:
        if item.get("analysis") is not None and summary_res["summarized_count"] == 0:
            item["is_reused"] = True

    is_pending = bool(summary_res["ai_pending"])
    final_status = RUN_STATUS_AI_PENDING if is_pending else RUN_STATUS_COMPLETED
    reused_only = summary_res["summarized_count"] == 0

    run_repo.save_run_articles(new_run.run_id, run_items)
    run_repo.finalize_run(
        run_id=new_run.run_id,
        status=final_status,
        fetched_count=0,
        deduped_count=len(candidates),
        ai_classified_count=0,
        ai_summarized_count=int(summary_res["summarized_count"]),
        displayed_count=len(selected_articles),
        used_provider=summary_res["used_provider"],
        used_model_id=summary_res["used_model_id"],
        reused_analysis=reused_only,
    )

    usage_agg = aggregate_run_token_usage(
        session=session,
        run_id=new_run.run_id,
        reused_analysis=reused_only,
    )
    return {
        "run_id": new_run.run_id,
        "parent_run_id": parent_id,
        "reused_analysis": reused_only,
        "displayed_count": len(selected_articles),
        "new_tokens_consumed": int(usage_agg["total_tokens"] or 0),
        "status": final_status,
    }


def resume_pending_ai_run(
    session: Session,
    run_id: str | None = None,
    gemini_provider: GeminiProvider | None = None,
    openrouter_provider: OpenRouterProvider | None = None,
) -> dict[str, Any]:
    """API障害や無料枠上限で保留（`ai_pending`）となった実行のAI処理を再開する（指示書 §12.3, §14, §20）。

    解説：
        1. 指定 `run_id`（省略時は最新の `ai_pending` 実行）を取得する。
        2. 既に保存されている記事群（RSS再取得は不要）に対し、未分析分のAI分類・AI要約を実行する。
        3. 元の実行を `parent_run_id` として参照する `retry` 実行を作成して結果を保存し、
           AI処理が完了した場合は元の `ai_pending` 実行も `completed` に更新する。

    Args:
        session (Session): DBセッション。
        run_id (str | None): 再開対象の `run_id`（省略時は最新の保留実行）。
        gemini_provider (GeminiProvider | None): テスト用 Gemini アダプタ。
        openrouter_provider (OpenRouterProvider | None): テスト用 OpenRouter アダプタ。

    Returns:
        dict[str, Any]: `resumed` (bool), `run_id`, `parent_run_id`, `status`, `summarized_count` 等を含む辞書。

    Side Effects:
        無料LLMの呼び出し、`news_analyses` / `news_runs` / `run_articles` の更新を行う。
    """
    run_repo = RunRepository(session)
    target_pending: NewsRun | None = None
    if run_id:
        candidate = run_repo.get_run(run_id)
        if candidate is not None and candidate.status == RUN_STATUS_AI_PENDING:
            target_pending = candidate
    else:
        pending_list = run_repo.list_pending_runs()
        target_pending = pending_list[0] if pending_list else None

    if target_pending is None:
        return {
            "resumed": False,
            "reason": "no_pending_run",
            "run_id": None,
            "status": "none",
        }

    setting = SettingRepository(session).get_or_create()
    cats = list(
        target_pending.selected_categories
        or setting.selected_categories
        or DEFAULT_SELECTED_CATEGORIES
    )

    retry_run = run_repo.create_run(
        run_type=RUN_TYPE_RETRY,
        selected_categories=cats,
        target_date=target_pending.target_date,
        parent_run_id=target_pending.run_id,
        reused_analysis=False,
    )

    pairs = run_repo.get_run_articles_with_details(target_pending.run_id)
    articles_in_run = [art for _, art in pairs]

    if not articles_in_run:
        art_repo = ArticleRepository(session)
        articles_in_run = art_repo.list_candidate_articles(
            selected_categories=cats,
            include_unclassified=True,
        )

    classify_res = classify_articles_with_llm(
        session=session,
        run_id=retry_run.run_id,
        articles=articles_in_run,
        gemini_provider=gemini_provider,
        openrouter_provider=openrouter_provider,
    )

    selected_articles = select_top_articles(
        candidates=articles_in_run,
        selected_categories=cats,
        max_count=setting.display_count,
        sort_order=setting.sort_order,
    )

    summary_res = summarize_selected_articles(
        session=session,
        run_id=retry_run.run_id,
        selected_articles=selected_articles,
        gemini_provider=gemini_provider,
        openrouter_provider=openrouter_provider,
    )

    still_pending = bool(classify_res["ai_pending"] or summary_res["ai_pending"])
    final_status = RUN_STATUS_AI_PENDING if still_pending else RUN_STATUS_COMPLETED
    used_prov = summary_res["used_provider"] or classify_res["used_provider"]
    used_mod = summary_res["used_model_id"] or classify_res["used_model_id"]

    run_repo.save_run_articles(retry_run.run_id, summary_res["run_items"])
    run_repo.finalize_run(
        run_id=retry_run.run_id,
        status=final_status,
        fetched_count=0,
        deduped_count=len(articles_in_run),
        ai_classified_count=int(classify_res["classified_count"]),
        ai_summarized_count=int(summary_res["summarized_count"]),
        displayed_count=len(selected_articles),
        used_provider=used_prov,
        used_model_id=used_mod,
    )

    # AI処理が完了した場合は元の保留実行も完了状態へ更新し、重複再試行を防ぐ
    if not still_pending:
        run_repo.save_run_articles(target_pending.run_id, summary_res["run_items"])
        run_repo.finalize_run(
            run_id=target_pending.run_id,
            status=RUN_STATUS_COMPLETED,
            fetched_count=target_pending.fetched_count,
            deduped_count=target_pending.deduped_count,
            ai_classified_count=int(classify_res["classified_count"]),
            ai_summarized_count=int(summary_res["summarized_count"]),
            displayed_count=len(selected_articles),
            used_provider=used_prov,
            used_model_id=used_mod,
        )

    return {
        "resumed": not still_pending,
        "run_id": retry_run.run_id,
        "parent_run_id": target_pending.run_id,
        "status": final_status,
        "classified_count": int(classify_res["classified_count"]),
        "summarized_count": int(summary_res["summarized_count"]),
        "displayed_count": len(selected_articles),
    }


def run_catchup_if_needed(
    session: Session,
    now_utc: datetime | None = None,
    http_client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Windows PCの停止・スリープ後に未取得分を補完取得する（指示書 §20）。

    解説：
        現在時刻（JST）が自動更新時刻（既定 07:00 JST）を過ぎており、
        かつ最終成功日時（`last_successful_run_at`）が本日の自動更新時刻より前（または未実行）
        である場合、停止中の未取得分を補完する `catchup` 実行を自動的に走らせる。

    Args:
        session (Session): DBセッション。
        now_utc (datetime | None): テスト用の現在時刻（UTC）。省略時は実現在時刻。
        http_client (httpx.Client | None): テスト用HTTPクライアント。

    Returns:
        dict[str, Any]: `catchup_executed` (bool) および実行結果を含む辞書。

    Side Effects:
        補完が必要な場合に `execute_digest_run(run_type="catchup")` を実行する。
    """
    setting = SettingRepository(session).get_or_create()
    if not setting.is_initial_approved or not setting.auto_update_enabled:
        return {"catchup_executed": False, "reason": "auto_update_disabled_or_unapproved"}

    current_utc = now_utc or datetime.now(UTC)
    if current_utc.tzinfo is None:
        current_utc = current_utc.replace(tzinfo=UTC)
    current_jst = current_utc.astimezone(JST_ZONE)

    try:
        parts = setting.auto_update_time.split(":")
        sched_hour, sched_min = int(parts[0]), int(parts[1])
    except Exception:
        sched_hour, sched_min = 7, 0

    today_scheduled_jst = datetime.combine(
        current_jst.date(), time(sched_hour, sched_min, 0), tzinfo=JST_ZONE
    )
    last_ok_utc = _ensure_utc(setting.last_successful_run_at)

    needs_catchup = False
    if last_ok_utc is None:
        needs_catchup = True
    else:
        last_ok_jst = last_ok_utc.astimezone(JST_ZONE)
        if current_jst >= today_scheduled_jst and last_ok_jst < today_scheduled_jst or (current_utc - last_ok_utc) >= timedelta(hours=24):
            needs_catchup = True

    if not needs_catchup:
        return {"catchup_executed": False, "reason": "up_to_date"}

    res = execute_digest_run(
        session=session,
        run_type=RUN_TYPE_CATCHUP,
        http_client=http_client,
    )
    return {
        "catchup_executed": True,
        "run_id": res.get("run_id"),
        "displayed_count": res.get("displayed_count", 0),
    }


def build_view_data_for_run(
    session: Session,
    run_id: str | None = None,
    display_count_override: int | None = None,
    sort_order_override: str | None = None,
) -> dict[str, Any]:
    """基本画面または履歴詳細画面に表示するニュースカード一覧と1実行サマリーを構築する。

    Args:
        session (Session): DBセッション。
        run_id (str | None): 表示対象の `run_id`（省略時は最新実行）。
        display_count_override (int | None): 表示件数の一時指定（1〜15）。
        sort_order_override (str | None): 並び順の一時指定（`importance` / `latest`）。

    Returns:
        dict[str, Any]: `cards` (list[dict]), `summary` (dict), `run` (NewsRun | None) を含む辞書。

    Side Effects:
        なし。
    """
    setting = SettingRepository(session).get_or_create()
    run_repo = RunRepository(session)
    art_repo = ArticleRepository(session)

    target_run: NewsRun | None = (
        run_repo.get_run(run_id) if run_id else run_repo.get_latest_displayable_run()
    )
    display_limit = int(display_count_override or setting.display_count or DEFAULT_DISPLAY_COUNT)
    sort_order = str(sort_order_override or setting.sort_order or "importance")

    if target_run is None:
        default_estimates = calculate_paid_model_estimates(
            session=session,
            total_input_tokens=0,
            total_output_tokens=0,
            measurement_state="unused",
        )
        empty_summary = build_run_summary_dict(
            {
                "run_id": "none",
                "executed_at_jst_str": "未実行",
                "run_type": "manual",
                "status": "completed",
                "paid_model_estimates": default_estimates,
            }
        )
        return {"cards": [], "summary": empty_summary, "run": None}

    pairs: list[tuple[RunArticle, NewsArticle]] = run_repo.get_run_articles_with_details(
        target_run.run_id
    )
    raw_cards: list[dict[str, Any]] = []
    for run_art, article in pairs:
        rel_count = art_repo.count_cluster_members(article.cluster_id)
        card_dict = build_article_card_dict(
            {
                "id": article.id,
                "title": article.title,
                "source_name": article.source_name,
                "url": article.url,
                "published_at": article.published_at,
                "published_at_jst_str": format_datetime_jst(article.published_at),
                "updated_at_rss": article.updated_at_rss,
                "updated_at_jst_str": (
                    format_datetime_jst(article.updated_at_rss)
                    if article.updated_at_rss
                    else None
                ),
                "category": run_art.snapshot_category or article.primary_category,
                "importance_score": run_art.snapshot_importance or article.importance_score,
                "summary_ja": run_art.snapshot_summary or "（AI要約未実行）",
                "why_important": run_art.snapshot_why_important or "（AI分析未実行）",
                "is_read": article.is_read,
                "is_reused": run_art.is_reused,
                "cluster_id": article.cluster_id,
                "related_sources_count": rel_count,
            }
        )
        raw_cards.append(card_dict)

    sorted_cards = sort_articles(raw_cards, sort_order=sort_order)[:display_limit]

    usage_agg = aggregate_run_token_usage(
        session=session,
        run_id=target_run.run_id,
        reused_analysis=target_run.reused_analysis,
    )
    paid_estimates = calculate_paid_model_estimates(
        session=session,
        total_input_tokens=usage_agg["total_input_tokens"],
        total_output_tokens=usage_agg["total_output_tokens"],
        measurement_state=str(usage_agg["measurement_state"]),
        total_reasoning_tokens=usage_agg["total_reasoning_tokens"],
        reasoning_included_in_output=True,
    )

    used_prov_str = (
        target_run.used_provider
        or usage_agg["used_provider"]
        or "未使用（RSS収集のみ）"
    )
    used_mod_str = (
        target_run.used_model_id
        or usage_agg["used_model_id"]
        or "未使用"
    )

    summary_dict = build_run_summary_dict(
        {
            "run_id": target_run.run_id,
            "parent_run_id": target_run.parent_run_id,
            "executed_at_jst_str": format_datetime_jst(target_run.started_at),
            "run_type": target_run.run_type,
            "fetched_count": target_run.fetched_count,
            "deduped_count": target_run.deduped_count,
            "ai_classified_count": target_run.ai_classified_count,
            "ai_summarized_count": target_run.ai_summarized_count,
            "displayed_count": len(sorted_cards),
            "status": target_run.status,
            "reused_analysis": target_run.reused_analysis,
            "used_provider": used_prov_str,
            "used_model": used_mod_str,
            "api_request_count": usage_agg["api_request_count"],
            "total_input_tokens": usage_agg["total_input_tokens"],
            "total_output_tokens": usage_agg["total_output_tokens"],
            "total_reasoning_tokens": usage_agg["total_reasoning_tokens"],
            "total_tokens": usage_agg["total_tokens"],
            "usage_measurement_status": usage_agg["usage_measurement_status"],
            "paid_model_estimates": paid_estimates,
        }
    )
    return {
        "cards": sorted_cards,
        "summary": summary_dict,
        "run": target_run,
    }


def validate_input(payload: dict[str, Any]) -> dict[str, Any]:
    """入力辞書を検証する。"""
    if "session" not in payload or not isinstance(payload["session"], Session):
        raise ValueError("有効な SQLAlchemy Session が必要です。")
    return dict(payload)


def execute_logic(validated: dict[str, Any]) -> dict[str, Any]:
    """指定されたアクションに応じてオーケストレータ処理を実行する。"""
    action = str(validated.get("action", "view"))
    session: Session = validated["session"]
    if action == "run":
        return execute_digest_run(
            session=session,
            run_type=str(validated.get("run_type", RUN_TYPE_MANUAL)),
            target_date=validated.get("target_date"),
            selected_categories=validated.get("selected_categories"),
        )
    if action == "recategorize":
        return recategorize_from_saved_articles(
            session=session,
            selected_categories=list(
                validated.get("selected_categories") or DEFAULT_SELECTED_CATEGORIES
            ),
        )
    return build_view_data_for_run(session=session, run_id=validated.get("run_id"))


def format_output(result: dict[str, Any]) -> dict[str, Any]:
    """結果辞書を整形して返す。"""
    return dict(result)


def main(payload: dict[str, Any]) -> dict[str, Any]:
    """オーケストレータの一連の処理を実行する。"""
    validated = validate_input(payload)
    res = execute_logic(validated)
    return format_output(res)
