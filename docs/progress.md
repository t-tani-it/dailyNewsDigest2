# dailyNewsDigest2 — 進捗管理（progress.md）

## 使い方（引き継ぎ手順）
別モデル・別セッションで作業を再開する場合は、次の順に読み込む。
1. `AGENTS.md`（プロジェクト規約・禁止事項）
2. `docs/progress.md`（本ファイル：完了済み/次回着手サブステップ）
3. `docs/handover.md`（直近の設計判断・実行結果・残課題・実行コマンド）

## 運用ルール
- 作業は**1サブステップ単位**で実施し、完了ごとに本ファイルのステータスを更新する。
- ステータス表記：`[ ] 未着手` / `[~] 進行中` / `[x] 完了`
- 各サブステップ完了報告時に、次のサブステップ向け**おすすめモデル（理由付き）**を提示する。
- フェーズ完了時に `docs/handover.md` を更新する。

## 現在地
- 現在のサブステップ：**Step 5-3 完了 / Phase 1〜4 全サブステップ完了 / 残確認事項を実行済み（外部環境待ちあり）**
- 最終更新日：2026-11-03

---

## Step 1: 準備・基盤構築
- [x] **Step 1-1**: プロジェクト管理・規約ファイル作成（2026-11-02 完了）
  - 成果物：`AGENTS.md`, `docs/progress.md`, `docs/handover.md`, `.gitignore`, `.env.example`
- [x] **Step 1-2**: 実行環境・インフラ定義作成（2026-11-02 完了）
  - 成果物：`requirements.txt`, `Dockerfile`, `compose.yaml`, `.venv` 作成・依存パッケージ導入
  - 完了条件：`.venv` 上で `pytest` / `uvicorn` / `alembic` が実行可能
  - 状態：**完了（2026-11-02）** — `requirements.txt`, `Dockerfile`, `compose.yaml`, `.dockerignore` 作成、`.venv` 構築・依存導入済み。`uvicorn 0.54.0` / `alembic 1.20.1` / `pytest 8.4.2` の実行を確認。Docker CLI は未導入のため `compose.yaml` の実起動検証は未実施。
- [x] **Step 1-3**: コア基盤実装（2026-11-02 完了）
  - 成果物：`app/core/config.py`, `database.py`, `security.py`, `logging.py`
  - 完了条件：設定値集約、SSRF対策URL検証、HTML無害化、APIキーマスクロガーの実装・疎通確認
- [x] **Step 1-4**: DBモデル（全10テーブル）とAlembicマイグレーション作成（2026-11-02 完了）
  - 成果物：`app/models/*.py`, `alembic.ini`, `alembic/env.py`, `alembic/versions/0001_initial.py`
  - 完了条件：全10テーブルのORM定義とマイグレーション整合性確認

## Step 2: Phase 1（RSS収集MVP）
- [x] **Step 2-1**: PydanticスキーマとRepository層の実装（2026-11-02 完了）
  - 成果物：`app/schemas/article_schema.py`, `run_schema.py`, `setting_schema.py`, `app/repositories/*.py`
- [x] **Step 2-2**: RSS収集・重複排除・記事更新検知・Python一次フィルタ（2026-11-02 完了）
  - 成果物：`app/services/rss_service.py`, `dedup_service.py`, `filter_service.py`, `selector_service.py`
- [x] **Step 2-3**: 初回確認フロー・基本画面・設定画面（2026-11-02 完了）
  - 成果物：`app/main.py`, `app/controllers/*`, `app/views/templates/*`, `static/css/style.css`, `static/js/app.js`
- [x] **Step 2-4**: 定期更新ワーカー・二重実行防止・停止後補完取得（2026-11-02 完了）
  - 成果物：`app/jobs/scheduler_worker.py`, `app/services/digest_orchestrator.py`
- [x] **Step 2-5**: Phase 1 テスト検証・コミット・ハンドオーバー更新（2026-11-02 完了）
  - 成果物：`tests/conftest.py`, `tests/test_rss_and_dedup.py`, `tests/test_views_and_onboarding.py`, `tests/test_security.py`
  - 検証：`pytest` 68件通過、`ruff check app alembic tests` エラー0件
  - 備考：Gitリポジトリ未初期化・リモート未設定のため、コミット／GitHub反映は最終ステップで実施

## Step 3: Phase 2（2日間のRSS取得量計測）
- [x] **Step 3-1**: 2日間計測ロジック・DB記録・暫定データ明示制御（2026-11-02 完了）
  - 成果物：`app/services/metrics_service.py`, `app/repositories/metrics_repo.py`
  - 内容：日次取得件数・重複率・失敗率・平均文字数・推定入力トークンの計算と保存、2日分未満は暫定判定、`execute_digest_run` / 初回確認からの計測記録フック
- [x] **Step 3-2**: 計測レポート画面・Phase 2テスト・ハンドオーバー更新（2026-11-02 完了）
  - 成果物：`app/views/templates/metrics.html`, `app/controllers/api_metrics_controller.py`, `GET /metrics`, `GET /api/metrics`, `POST /api/metrics/measure`, `tests/test_metrics.py`
  - 検証：`pytest` 80件通過、`ruff check app tests` エラー0件

## Step 4: Phase 3（AI機能・無料モデル管理・参考料金算出）
- [x] **Step 4-1**: 共通PydanticスキーマとGemini/OpenRouter Adapter（2026-11-03 完了）
  - 成果物：`app/schemas/llm_schema.py`, `app/providers/__init__.py`, `app/providers/base_provider.py`, `gemini_provider.py`, `openrouter_provider.py`
  - 検証：`ruff check app alembic tests` エラー0件、`pytest` 80件通過、有料比較モデル遮断・無料モデル判定確認済み
- [x] **Step 4-2**: 無料モデル自動発見・許可リスト照合・無料枠監視・自動切替（2026-11-03 完了）
  - 成果物：`app/services/model_catalog_service.py`, `app/services/model_router_service.py`, `app/controllers/api_setting_controller.py` 更新
  - 検証：`ruff check app alembic tests` エラー0件、`pytest` 80件通過
- [x] **Step 4-3**: AIカテゴリ分類・重要度判定・最大15件選定・日本語要約（2026-11-03 完了）
  - 成果物：`app/services/classifier_service.py`, `app/services/summarizer_service.py`, `app/services/digest_orchestrator.py` 連携
  - 検証：`ruff check app alembic tests` エラー0件、`pytest` 80件通過
- [x] **Step 4-4**: `run_id` トークン使用量記録・集計・有料3モデル参考料金計算（2026-11-03 完了）
  - 成果物：`app/services/usage_service.py`, `app/services/pricing_service.py`, `app/services/digest_orchestrator.py` サマリー連携
  - 検証：`ruff check app alembic tests` エラー0件、`pytest` 80件通過
- [x] **Step 4-5**: LLMモデル選択UI・AI処理/使用量サマリー・Phase 3テスト（2026-11-03 完了）
  - 成果物：サマリーUI（`app/views/templates/index.html`, `app/views/static/js/app.js`）, `tests/test_llm_and_routing.py`, `tests/test_usage_and_pricing.py`
  - 検証：`pytest` 105件通過、`ruff check app alembic tests` エラー0件

## Step 5: Phase 4（運用品質向上・README・仕上げ）
- [x] **Step 5-1**: カテゴリ変更時の既存分析再利用・API障害時の保留/再開（2026-11-03 完了）
  - 成果物：`app/services/digest_orchestrator.py`（`recategorize_from_saved_articles` 拡張、`resume_pending_ai_run` 追加）、`app/controllers/api_news_controller.py`（`POST /api/news/retry-pending`）、`tests/test_llm_and_routing.py`
  - 検証：`pytest` 105件通過、`ruff check app alembic tests` エラー0件
- [x] **Step 5-2**: 過去実行履歴画面・使用量ダッシュボード（2026-11-03 完了）
  - 成果物：`app/views/templates/history.html`, `usage.html`, `app/services/usage_dashboard_service.py`, `app/controllers/api_usage_controller.py`, `GET /usage`, `GET /api/usage`, `tests/test_usage_dashboard.py`
  - 内容：run_id別トークン表示、未計測と0の区別、モデル別集計、自動切替履歴、有料3モデル単価確認状態、実行別サマリー
  - 検証：`pytest` 113件通過、`ruff check app alembic tests` エラー0件
- [x] **Step 5-3**: 全32項目テスト最終確認・README整備・最終ハンドオーバー・GitHub反映（2026-11-03 完了）
  - 成果物：`README.md`, `docs/test_matrix.md`, `docs/handover.md`, `docs/progress.md`
  - 内容：要件32項目とテストの対応表、PowerShell/cmd両対応のセットアップ・起動手順、無料LLMポリシー、API一覧、既知の制約、最終ハンドオーバー
  - GitHub：`main` ブランチで論理的な複数コミットに分割し、publicリポジトリへpush
  - 検証：`pytest` 113件通過、`ruff check app alembic tests` エラー0件、32/32項目確認済み

## 残確認事項の実行記録（2026-11-03）
- [x] PostgreSQL 18の一時クラスタ（ポート55432、trust認証）で `alembic upgrade head` を実行し、`alembic_version` を含む11テーブルの作成を確認。
- [x] PostgreSQL接続でFastAPIを起動し、`/health` の `db_connected=true`、主要画面（`/`、`/usage`、`/metrics`、`/history`、`/settings`）のHTTP 200を確認。
- [x] `alembic.ini` のcp932 `UnicodeDecodeError` をASCII化で修正し、`alembic upgrade head --sql` のオフラインSQL生成とオンライン適用を確認。
- [x] OpenRouter公開モデル一覧APIのHTTP 200、`:free` 14件、`openrouter/free` の存在を確認。
- [x] Gemini APIの到達性（APIキー無しでHTTP 403）を確認。
- [ ] Docker ComposeによるPostgreSQL 16の起動（Docker CLI未導入）。
- [ ] 実際のGemini / OpenRouter API呼び出し（APIキー未設定）。
- [ ] 同一Wi-Fi内の実機スマートフォンからの表示（PCのLAN IP経由の同一ホストアクセスはHTTP 200、Firewall設定待ち）。

---

## Step 1-1 完了記録
- 実施日：2026-11-02
- 作成ファイル：
  - `AGENTS.md`（プロジェクト永続ルール。公開禁止情報・LLM無料ポリシー・コーディング規約を明記）
  - `docs/progress.md`（本ファイル）
  - `docs/handover.md`（ハンドオーバー記録）
  - `.gitignore`（`.venv/`, `.env`, 生成物を除外）
  - `.env.example`（APIキーはダミー空値、有料モデル実行禁止を明記）
- 検証：公開禁止情報（ローカル絶対パス・実APIキー・個人ID）を含まないことを確認済み。

## Step 1-2 完了記録
- 実施日：2026-11-02
- 作成ファイル：
  - `requirements.txt`（FastAPI / SQLAlchemy / Alembic / psycopg / Pydantic / feedparser / httpx / google-genai / APScheduler / pytest / ruff）
  - `Dockerfile`（Python 3.11-slim、非rootユーザー、`/health` ヘルスチェック）
  - `compose.yaml`（db / app / worker の3サービス。Webとスケジューラを分離、DBは `127.0.0.1` バインド）
  - `.dockerignore`（`.env`, `.venv`, `tests`, `docs` 等を除外）
  - `.venv`（Python 3.11.15、依存導入済み）
- 検証：
  - `uvicorn 0.54.0` / `alembic 1.20.1` / `pytest 8.4.2` の実行確認
  - **Docker CLI 未導入のため `compose.yaml` の実起動検証は未実施**（環境にDocker Desktop導入後に要確認）
- 未確認事項：`google-genai` 1.75.0 のAPI仕様、PostgreSQL 16 との接続（DB未起動）
- 既知の注意点：`.venv` の基底インタプリタが PATH 上の別プロジェクト venv 由来（Python 3.11.15）。パッケージは独立しているが、参照元削除時に再作成が必要。詳細は `docs/handover.md` を参照。

## Step 1-3 〜 Step 2-4 完了記録
- 実施日：2026-11-02
- 作成・実装ファイル：
  - **Step 1-3（コア基盤）**：`app/core/config.py`, `app/core/logging.py`, `app/core/security.py`, `app/core/database.py`
  - **Step 1-4（DBモデル・Alembic）**：`app/models/rss_source.py`, `article.py`, `analysis.py`, `run.py`, `usage_log.py`, `model_catalog.py`, `setting.py`, `metrics.py`, `alembic.ini`, `alembic/env.py`, `alembic/versions/0001_initial.py`
  - **Step 2-1（スキーマ・Repository）**：`app/schemas/article_schema.py`, `run_schema.py`, `setting_schema.py`, `app/repositories/rss_source_repo.py`, `article_repo.py`, `run_repo.py`, `setting_repo.py`
  - **Step 2-2（RSS収集・重複排除・一次フィルタ・選定）**：`app/services/filter_service.py`, `dedup_service.py`, `rss_service.py`, `selector_service.py`
  - **Step 2-3（Controller・View・基本/初回/履歴/設定画面）**：`app/main.py`, `app/controllers/api_news_controller.py`, `api_setting_controller.py`, `view_controller.py`, `app/views/templates/base.html`, `onboarding.html`, `index.html`, `history.html`, `settings.html`, `app/views/static/css/style.css`, `app/views/static/js/app.js`
  - **Step 2-4（定期実行・二重実行防止・停止後補完取得）**：`app/services/digest_orchestrator.py`, `app/jobs/scheduler_worker.py`, `pyproject.toml`
- 検証：
  - `ruff check app alembic`：All checks passed!（エラー0件）
  - 全10テーブル生成・FastAPI `TestClient`（`/health`, `/` 初回確認画面）・APScheduler 7:00 Asia/Tokyo ジョブ登録のスモーク検証通過。

## Step 2-5 完了記録
- 実施日：2026-11-02
- 作成ファイル：
  - `tests/conftest.py`（テストごとに独立した一時SQLiteファイルDB、`db_session`、`client`）
  - `tests/test_rss_and_dedup.py`（RSS成功・失敗・利用条件未確認、全件保持、URL/GUID重複、別メディアクラスタ、記事更新検知、JST日境界、公開日時不明記事、範囲外通知、URL正規化）
  - `tests/test_views_and_onboarding.py`（初回確認のはい/いいえ/キャンセル、プレビュー未保存、最大15件、表示件数、カテゴリOR、クラスタ代表1件、並び替え、手動更新、履歴API/画面、既読API、補完取得、二重実行防止）
  - `tests/test_security.py`（SSRF拒否、URL正規化、HTML無害化、プロンプトインジェクション、ローカル判定、不正JSON 422、有料LLM有効化400、SSRF RSS登録400）
- 検証結果：`pytest` 68 passed / `ruff check app alembic tests` エラー0件
- 修正点：`sanitize_html_text` のdocstring例を実装（タグを空白区切りに置換）に合わせて修正。

## Step 3-1 完了記録
- 実施日：2026-11-02
- 作成ファイル：
  - `app/repositories/metrics_repo.py`（日次計測の取得・同一日上書き・一覧・実測日数集計）
  - `app/services/metrics_service.py`（日次計測値の計算、RSSのみ計測実行、2日分実測判定、レポート集計）
- 内容：
  - 重複率 = (取得件数 - 重複排除後件数) / 取得件数
  - カテゴリ別件数・平均タイトル文字数・平均概要文字数・推定入力トークン（`ESTIMATED_TOKENS_PER_CHAR = 0.85`）
  - 2日分未満はレポート全体を「暫定（n/2日）」、2日分で「実測完了（2日分）」
  - `execute_digest_run` と初回確認「はい」から日次計測を記録。計測失敗時は警告のみでニュース処理を継続
- 検証結果：`pytest` 76 passed / `ruff check app tests` エラー0件

## Step 3-2 完了記録
- 実施日：2026-11-02
- 作成ファイル：
  - `app/views/templates/metrics.html`（実測進捗、集計値、カテゴリ別件数、日次明細、計測実行ボタン）
  - `app/controllers/api_metrics_controller.py`（`GET /api/metrics`, `POST /api/metrics/measure`）
  - `app/controllers/view_controller.py` に `GET /metrics` 追加
  - `app/views/templates/base.html` に「RSS計測」ナビ追加
  - `app/views/static/js/app.js` に計測実行ハンドラ追加
  - `tests/test_metrics.py`（計測計算、同一日上書き、1日/2日判定、未計測、RSSのみ実行、通常実行フック、API/画面）
- 検証結果：`pytest` 80 passed / `ruff check app tests` エラー0件

## Step 4-1 完了記録
- 実施日：2026-11-03
- 作成ファイル：
  - `app/schemas/llm_schema.py`（AIカテゴリ分類・重要度・除外判定スキーマ、AI日本語要約・「なぜ重要なのか」・「概要情報不足」統一スキーマ、未計測を0と混同せず推論トークン二重計上を防ぐ `LlmTokenUsageSchema`、Provider統一応答スキーマ、JSON抽出・検証ヘルパー）
  - `app/providers/base_provider.py`（`BaseLlmProvider` 抽象基底クラス、無料ポリシー強制ガード `ensure_free_execution_allowed`、上限付きJSON再試行制御、エラー種別分類 `classify_provider_exception`）
  - `app/providers/gemini_provider.py`（`google-genai` アダプタ、Free Tier確認フラグ＋許可リスト照合、`models.list` 取得、`usage_metadata` 抽出）
  - `app/providers/openrouter_provider.py`（OpenRouter `/chat/completions`・`/models` アダプタ、`:free` / `openrouter/free` かつ料金0検証、1Mトークン単価換算、`usage` 抽出）
  - `app/providers/__init__.py`, `app/schemas/__init__.py` 更新
- 検証結果：`ruff check app alembic tests` エラー0件 / `pytest` 80 passed

## Step 4-2 完了記録
- 実施日：2026-11-03
- 作成・更新ファイル：
  - `app/services/model_catalog_service.py`（初期無料モデル候補と有料比較3モデルのシード、Google AI Studio / OpenRouter モデル一覧からの無料判定同期、比較用有料モデルの公式単価スナップショット更新、レート制限等発生時の可用性ステータス更新）
  - `app/services/model_router_service.py`（JST当日のAPI呼び出し回数監視、選択モード `auto`/`gemini`/`openrouter`・優先モデル・自動切替設定に基づく候補順リスト構築、失敗時の自動フォールバック・`llm_usage_logs` 記録・全無料モデル失敗時の `ai_pending` 安全停止）
  - `app/controllers/api_setting_controller.py`（`GET /api/models`, `POST /api/models/refresh` を `model_catalog_service` に接続）
- 検証結果：`ruff check app alembic tests` エラー0件 / `pytest` 80 passed

## Step 4-3 完了記録
- 実施日：2026-11-03
- 作成・更新ファイル：
  - `app/services/classifier_service.py`（プロンプトインジェクション無害化付きAI分類プロンプト構築、バッチ分割による無料モデル呼び出し、記事単位の除外判定と `unclassified` 安全維持、既存分析再利用、LLM無効・全モデル失敗時のPython一次分類維持）
  - `app/services/summarizer_service.py`（選定済み最大15件限定のAI日本語要約・「なぜ重要なのか」生成、既存分析再利用、配信元 `allow_ai_summary` 遵守、概要欠落・情報不足時の推測禁止と `"概要情報不足"` 保存、AI失敗時の `ai_pending` 保留制御）
  - `app/services/digest_orchestrator.py`（初回確認「はい」および `execute_digest_run` にAI分類・選定・AI要約パイプラインを接続）
- 検証結果：`ruff check app alembic tests` エラー0件 / `pytest` 80 passed

## Step 4-4 完了記録
- 実施日：2026-11-03
- 作成・更新ファイル：
  - `app/services/usage_service.py`（`run_id` 単位のトークン使用量集計、推論トークン二重加算防止、未計測 `None` と `0` の分離、一部未計測 `partial_unmeasured` 判定、ダッシュボード向けモデル別・自動切替履歴集計）
  - `app/services/pricing_service.py`（比較対象有料3モデルの参考料金計算、確認日時付き価格スナップショット管理、価格未確認・トークン未計測時の `"算出不可"` 判定、小額の `$0.00` 表示禁止）
  - `app/services/digest_orchestrator.py`（`build_view_data_for_run` に1実行単位のトークン集計と有料3モデル参考料金を接続）
- 検証結果：`ruff check app alembic tests` エラー0件 / `pytest` 80 passed

## Step 4-5 & Step 5-1 完了記録
- 実施日：2026-11-03
- 作成・更新ファイル：
  - `app/views/templates/index.html`, `app/views/static/js/app.js`（基本画面のLLM選択モード連動フィルタ・即時保存・推論トークン内訳表示・`ai_pending` 時の「保留中のAI処理を再開」ボタン）
  - `app/services/digest_orchestrator.py`（`recategorize_from_saved_articles` における既存分析優先再利用と新規トークン消費0制御、API障害時の保留実行を再開する `resume_pending_ai_run` 実装）
  - `app/controllers/api_news_controller.py`（`POST /api/news/retry-pending` 追加）
  - `tests/test_llm_and_routing.py`（共通スキーマ検証、有料3モデル遮断、Free Tier未確認・非`:free`遮断、JSONリトライ上限、モデル発見・単価同期、429レート制限時の別プロバイダー無料モデル自動切替、1日上限停止、カテゴリ変更時の再利用トークン0、`ai_pending` 保留からの再開テスト）
  - `tests/test_usage_and_pricing.py`（`run_id` トークン合算、推論トークン二重加算防止、未計測 `None` と `0` の分離、一部未計測、比較対象3モデルの参考料金計算・未確認時 `"算出不可"`・小額 `$0.00` 禁止、サマリー1箇所表示テスト）
- 検証結果：`pytest` 105 passed / `ruff check app alembic tests` エラー0件

## Step 5-2 完了記録
- 実施日：2026-11-03
- 作成・更新ファイル：
  - `app/services/usage_service.py`（`summarize_usage_logs` による直近ログ全体の合計・計測状態集計、`list_recent_run_usage` による実行別トークン集計、モデル別の計測済み/未計測件数）
  - `app/services/pricing_service.py`（`get_price_snapshot_rows` / `format_unit_price` による有料3モデル単価・確認状態・確認日時・取得元URLの表示用整形）
  - `app/services/usage_dashboard_service.py`（ダッシュボード構成、履歴一覧用 `run_id` 別集計マップ）
  - `app/controllers/api_usage_controller.py`（`GET /api/usage`、未知 `run_id` は404）
  - `app/controllers/view_controller.py`（`GET /usage` 追加、履歴画面へ実行別トークン集計とラベルを提供）
  - `app/views/templates/usage.html`（使用量ダッシュボード：当日上限、モデル別、実行別、切替履歴、参考単価、直近ログ）
  - `app/views/templates/history.html`（トークン列・AI処理列・状態ラベル・使用モデル列を追加、未計測表示を修正）
  - `app/views/templates/base.html`（「AI使用量」ナビ追加）、`app/views/static/css/style.css`（統計カード等）
  - `tests/test_usage_dashboard.py`（8テスト追加）
- 検証結果：`pytest` 113 passed / `ruff check app alembic tests` エラー0件

## 未完了・注意事項
- Gitリポジトリは初期化済み。`main` ブランチで論理的な複数コミットに分割し、publicリポジトリへpush済み。
- Docker CLI 未導入のため、PostgreSQL 16 の実接続・Alembic マイグレーション実行は未検証。PostgreSQL 18の一時クラスタでのオンライン適用は確認済み。
- Phase 2 の実測は「実行した日」のみ記録される。2日分の実測を確定するには、別日に `POST /api/metrics/measure` または通常更新を実行する必要がある。
- 実際のGemini / OpenRouter無料APIのレート制限・無料枠挙動は未確認。OpenRouter公開モデル一覧とGemini APIの到達性は確認済み。`.env`にAPIキーを設定して実呼び出しを確認する必要がある。
- 同一Wi-Fi内スマートフォンからの実機表示は未確認。PCのLAN IP経由の同一ホストアクセスは主要画面HTTP 200。Windows Firewallの受信許可（TCP 8000、同一サブネット限定）が必要な場合がある。
- `alembic.ini` はWindows cp932対策としてASCIIのみで維持する。
- 初期版は認証機能を持たないため、インターネットへ公開しないこと。
