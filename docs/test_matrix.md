# dailyNewsDigest2 — 要件32項目テスト確認表（test_matrix.md）

## 1. 目的

本ファイルは、開発指示書 v3.0 の主要要件を32項目に整理し、それぞれが自動テストで確認されているかを追跡するための確認表である。

- 確認日：2026-11-03
- テスト実行：`pytest`
- Lint実行：`ruff check app alembic tests`
- 結果：**32 / 32 項目 確認済み**
- 自動テスト：**113 passed**
- Lint：**All checks passed**

## 2. 確認方法

1. 各要件に対応するテスト関数を `tests/` 配下に用意する。
2. `pytest` を実行し、全テストが成功することを確認する。
3. `ruff check app alembic tests` を実行し、静的解析エラーが0件であることを確認する。
4. 外部LLMの実APIは呼び出さず、モックまたはテスト用データで検証する。
5. 本番用PostgreSQLには接続せず、テストごとの一時SQLiteファイルDBを使用する。

## 3. 32項目確認表

| No | 確認項目 | 主なテスト | 結果 |
|---:|---|---|---|
| 1 | RSSの新着・更新記事を15件に切り詰めず全件取得する | `test_parse_rss_xml_content_keeps_all_entries` | 合格 |
| 2 | RSSのHTTP失敗・利用条件未確認ソースを安全に停止する | `test_fetch_single_feed_http_error_returns_failure`, `test_fetch_single_feed_rejects_unverified_source` | 合格 |
| 3 | 正規化URL・GUIDによる重複排除を行う | `test_dedup_removes_same_normalized_url_and_guid`, `test_upsert_articles_does_not_reinsert_same_guid` | 合格 |
| 4 | 別メディアの同一イベント記事をクラスタリングする | `test_dedup_clusters_same_event_from_different_sources`, `test_dedup_keeps_distinct_articles` | 合格 |
| 5 | 既存記事の更新を検知し、重複再登録しない | `test_upsert_articles_detects_new_duplicate_and_update` | 合格 |
| 6 | JST（Asia/Tokyo）の日境界をUTCへ正しく変換する | `test_jst_day_bounds_are_utc_converted`, `test_filter_items_by_target_date_uses_jst_boundaries` | 合格 |
| 7 | 公開日時が不明な記事を対象日内と断定しない | `test_filter_items_by_target_date_excludes_unverified_pubdate` | 合格 |
| 8 | RSSの公開範囲外であることを利用者へ通知する | `test_filter_items_by_target_date_reports_available_range` | 合格 |
| 9 | 初回確認「はい」で記事保存・自動更新を有効化する | `test_onboarding_yes_saves_articles_and_enables_auto_update` | 合格 |
| 10 | 初回確認「いいえ」でタイトルのみ表示しDB保存しない | `test_onboarding_no_decision_keeps_titles_only` | 合格 |
| 11 | 初回確認「キャンセル」でプレビューを破棄しDB保存しない | `test_onboarding_cancel_discards_preview`, `test_onboarding_preview_does_not_save_articles` | 合格 |
| 12 | 最大15件・ユーザー指定の表示件数を守る | `test_select_top_articles_caps_at_15`, `test_select_top_articles_respects_display_count` | 合格 |
| 13 | カテゴリOR条件・重要度順/最新順の並び替えを守る | `test_select_top_articles_excludes_pr_and_filters_categories`, `test_sort_articles_importance_and_latest` | 合格 |
| 14 | 手動ニュース更新を実行し、実行履歴へ記録する | `test_manual_refresh_api_and_run_history` | 合格 |
| 15 | 実行履歴API・画面でrun_id単位の結果を参照できる | `test_manual_refresh_api_and_run_history`, `test_history_page_displays_unmeasured_instead_of_none_tokens` | 合格 |
| 16 | 記事の既読/未読を切り替えられる | `test_article_read_status_api` | 合格 |
| 17 | 定期実行停止後の補完取得と当日成功時のスキップを行う | `test_catchup_runs_when_scheduled_time_missed`, `test_catchup_skips_when_already_succeeded_today` | 合格 |
| 18 | 二重実行ロックで同時実行を防止する | `test_run_lock_prevents_double_execution` | 合格 |
| 19 | SSRFを拒否し、URLを正規化する | `test_validate_safe_url_rejects_internal_or_unsafe_urls`, `test_normalize_url_strips_tracking_params_and_fragment` | 合格 |
| 20 | HTMLを無害化し、プロンプトインジェクションを無効化する | `test_sanitize_html_text_removes_dangerous_tags_and_decodes_entities`, `test_sanitize_for_llm_prompt_filters_english_injection`, `test_sanitize_for_llm_prompt_filters_japanese_injection` | 合格 |
| 21 | ローカル判定・不正JSON 422・有料LLM有効化400を守る | `test_is_local_or_private_client_rejects_external`, `test_malformed_json_returns_422`, `test_paid_llm_activation_is_rejected` | 合格 |
| 22 | 日次RSS計測値（件数・重複率・失敗率・平均文字数・推定トークン）を計算する | `test_build_daily_metric_data_calculates_rates_and_counts`, `test_build_daily_metric_data_handles_empty_collection` | 合格 |
| 23 | 同一日の再計測を加算せず最新スナップショットで上書きする | `test_record_daily_metric_overwrites_same_date` | 合格 |
| 24 | 1日/2日判定・未計測表示を正しく行う | `test_report_is_provisional_after_one_day`, `test_report_is_complete_after_two_days`, `test_report_without_data_shows_unmeasured` | 合格 |
| 25 | RSSのみ計測ではLLMを呼ばず、記事本文も保存しない | `test_run_rss_measurement_saves_metric_without_articles` | 合格 |
| 26 | 通常実行からの計測記録と計測API/画面を提供する | `test_digest_run_records_daily_metric`, `test_metrics_api_returns_report`, `test_metrics_measure_api_records_and_reflects_report`, `test_metrics_page_shows_unmeasured` | 合格 |
| 27 | 有料3モデルのAPI呼び出しを常時遮断する | `test_providers_block_all_paid_comparison_models`, `test_paid_llm_activation_is_rejected` | 合格 |
| 28 | 無料モデル発見・自動切替・1日上限・安全停止を守る | `test_refresh_model_catalog_filters_free_vs_unverified_and_syncs_prices`, `test_router_switches_from_rate_limited_gemini_to_openrouter_free`, `test_router_stops_when_daily_call_limit_reached` | 合格 |
| 29 | run_id単位のトークン集計と推論トークン二重計上防止を守る | `test_aggregate_run_token_usage_sums_requests_and_prevents_double_counting`, `test_compute_single_model_reference_cost_handles_separate_reasoning_tokens` | 合格 |
| 30 | 参考料金の「算出不可」と極小金額の非$0.00表示を守る | `test_calculate_paid_model_estimates_returns_uncalculable_when_price_unverified`, `test_calculate_paid_model_estimates_returns_uncalculable_when_tokens_unmeasured`, `test_format_usd_cost_never_displays_zero_point_zero_zero_for_tiny_amounts` | 合格 |
| 31 | カテゴリ変更時の分析再利用とai_pending再開を守る | `test_digest_run_with_llm_and_recategorize_reuses_analysis_with_zero_new_tokens`, `test_ai_pending_fallback_and_resume_pending_run` | 合格 |
| 32 | 使用量ダッシュボード・履歴画面で未計測を0と誤表示しない | `test_usage_page_renders_dashboard_and_policy_notice`, `test_usage_api_returns_json_and_rejects_unknown_run`, `test_history_page_displays_unmeasured_instead_of_none_tokens` | 合格 |

## 4. テストファイル一覧

| ファイル | 主な対象 |
|---|---|
| `tests/test_rss_and_dedup.py` | RSS取得、重複排除、クラスタリング、更新検知、JST日境界、URL正規化 |
| `tests/test_views_and_onboarding.py` | 初回確認、選定、表示件数、カテゴリ、手動更新、履歴、既読、補完取得、二重実行防止 |
| `tests/test_security.py` | SSRF、HTML無害化、プロンプトインジェクション、ローカル判定、入力検証 |
| `tests/test_metrics.py` | Phase 2の日次計測、2日判定、RSSのみ計測、計測API/画面 |
| `tests/test_llm_and_routing.py` | 無料ポリシー、モデル発見、自動切替、日次上限、分析再利用、ai_pending再開 |
| `tests/test_usage_and_pricing.py` | run_idトークン集計、推論二重計上防止、参考料金、小額表示 |
| `tests/test_usage_dashboard.py` | 使用量ダッシュボード、履歴画面の未計測表示、参考単価、API/画面 |

## 5. 未確認事項

以下は自動テストの対象外であり、実環境での確認が必要である。

- Docker ComposeによるPostgreSQL 16の起動
- PostgreSQLへのAlembicマイグレーション適用（`alembic upgrade head`）
- 実際のGemini API / OpenRouter APIを使った無料枠・レート制限の挙動
- 実際のRSS配信元の利用規約・配信仕様の変動
- 同一Wi-Fi内のスマートフォンからの実機表示

## 6. 変更履歴

- 2026-11-03：初版作成（Step 5-3）。32項目の対応表と113テストの結果を記録。
