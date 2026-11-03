# dailyNewsDigest2 — ハンドオーバー記録（handover.md）

> 別モデル・別セッションがコンテキスト0%から再開するための記録。
> 更新タイミング：フェーズ完了時（必須）／重要な設計判断を行った時。

---

## 1. プロジェクト方針（計画）

### 目的
毎朝、国内外のニュースをRSSから自動収集し、ユーザーが選択したカテゴリの重要ニュースを最大15件選定・要約して表示する個人向けWebアプリ。ChatGPT Plus の情報収集機能の代替。

### 確定している設計判断
| 項目 | 決定内容 | 理由 |
|---|---|---|
| アーキテクチャ | MVC + Service + Repository + Provider(Adapter) | Controllerへのロジック混入防止、Gemini/OpenRouter差分の局所化 |
| DB | PostgreSQL（本番・Compose）、SQLite（テスト） | 指示書必須要件とテスト実行容易性の両立 |
| 日時 | UTC保存・表示時 `Asia/Tokyo` 変換 | タイムゾーン情報保持の要件 |
| 実行単位 | `news_runs.run_id`（UUID）でトークン集計 | 「実行単位で集計」要件 |
| 分析参照 | `run_articles.analysis_id` で表示時点の分析を参照 | 過去実行結果の不変性確保 |
| 無料判定 | `model_catalog.free_tier_status` が `verified_free` のみ実行可 | 有料・条件付きモデルの実行防止 |
| 参考料金 | `price_snapshots` に確認日時付きで保持。未確認は「算出不可」 | 公式価格のハードコード防止 |
| 計測期間 | Phase 2 は **2日間**（指示書の7日間を読み替え） | ユーザー指示 |
| 再選別 | 保存済み分析を再利用し、新規トークン消費0 | カテゴリ変更時の過剰API呼び出し防止 |
| 収集とAI | 完全分離。LLM不可でもRSS収集・閲覧は継続 | 指示書 §14 |

### 重要な禁止事項
- 有料モデルのAPI呼び出し（比較3モデルは参考料金計算のみ）
- 料金・無料枠の恒久的ハードコード
- 公開禁止情報（ローカル絶対パス・APIキー・個人ID）の混入
- LLMの無限リトライ
- RSSの利用条件未確認ソースの自動使用
- 記事全文の自動取得

---

## 2. 実装（実行）

### 実施済み
| 日付 | サブステップ | 内容 |
|---|---|---|
| 2026-11-02 | Step 1-1 | `AGENTS.md`, `docs/progress.md`, `docs/handover.md`, `.gitignore`, `.env.example` 作成 |
| 2026-11-02 | Step 1-2 | `requirements.txt`, `Dockerfile`, `compose.yaml`, `.dockerignore` 作成、`.venv` 構築・依存導入 |
| 2026-11-02 | Step 1-3 | `app/core/config.py`, `logging.py`, `security.py`, `database.py` 実装 |
| 2026-11-02 | Step 1-4 | SQLAlchemy 全10テーブルモデル（`app/models/*.py`）および Alembic 初期マイグレーション作成 |
| 2026-11-02 | Step 2-1 | Pydanticスキーマ（`app/schemas/*.py`）および Repository 層（`app/repositories/*.py`）実装 |
| 2026-11-02 | Step 2-2 | RSS収集・重複排除・別メディア関連記事クラスタリング・Python一次フィルタ・最大15件選定の実装 |
| 2026-11-02 | Step 2-3 | FastAPI Controller 層・Jinja2 テンプレート（初回確認 / 基本画面 / 履歴 / 設定）・レスポンシブ CSS / JS 実装 |
| 2026-11-02 | Step 2-4 | 定期実行ワーカー（毎朝07:00 Asia/Tokyo）・二重実行防止ロック・停止後補完取得（`scheduler_worker.py`, `digest_orchestrator.py`）実装 |
| 2026-11-02 | Step 2-5 | Phase 1 テスト一式（RSS/重複排除/更新検知/対象日、初回確認/選定/履歴/既読/補完取得、セキュリティ）を作成し68件通過 |
| 2026-11-02 | Step 3-1 | 日次RSS計測Repository/Service、2日分実測判定、通常更新・初回確認からの計測記録フックを実装 |
| 2026-11-02 | Step 3-2 | 計測レポート画面（`/metrics`）・計測API（`GET /api/metrics`, `POST /api/metrics/measure`）・Phase 2テストを追加 |
| 2026-11-03 | Step 4-1 | 共通Pydanticスキーマ（`llm_schema.py`）および Gemini / OpenRouter Provider（無料利用ポリシー強制ガード・回数制限リトライ）実装 |
| 2026-11-03 | Step 4-2 | 無料モデル自動発見・許可リスト照合（`model_catalog_service.py`）および無料枠監視・自動モデル切替ルーター（`model_router_service.py`）実装 |
| 2026-11-03 | Step 4-3 | AIカテゴリ分類・重要度判定（`classifier_service.py`）および最大15件限定のAI日本語要約（`summarizer_service.py`）実装・オーケストレータ連携 |
| 2026-11-03 | Step 4-4 | `run_id` 単位トークン使用量集計（`usage_service.py`）および比較対象有料3モデル参考料金計算（`pricing_service.py`）実装 |
| 2026-11-03 | Step 4-5 | 基本画面のLLM選択UI連動・サマリー推論内訳表示、`tests/test_llm_and_routing.py` / `tests/test_usage_and_pricing.py` 作成 |
| 2026-11-03 | Step 5-1 | カテゴリ変更時の既存分析再利用（新規トークン消費0）および `ai_pending` 保留実行の再開処理（`resume_pending_ai_run`, `POST /api/news/retry-pending`）実装 |
| 2026-11-03 | Step 5-2 | 過去実行履歴のトークン表示拡充、AI使用量ダッシュボード（`GET /usage`, `GET /api/usage`）、モデル別・実行別・切替履歴・有料3モデル単価確認状態を実装 |
| 2026-11-03 | Step 5-3 | 要件32項目のテスト確認表、README、最終ハンドオーバー、Git初期化・分割コミット・GitHub公開を実施 |

### 未実施
- Docker Desktop導入後のPostgreSQL 16実接続・`alembic upgrade head`の実機確認
- 実際のGemini / OpenRouter無料APIを用いたレート制限・無料枠挙動の確認
- 同一Wi-Fi内スマートフォンからの実機表示確認

---

## 3. 結果

### Step 1-1 の成果
- プロジェクト規約を `AGENTS.md` に集約（MVC構成、コーディング規約、セキュリティ、LLM無料ポリシー、GitHub運用）。
- `.gitignore` で `.venv/`, `venv/`, `.env`, キャッシュ類、SQLiteファイルを除外。
- `.env.example` に必要な環境変数の雛形を定義（APIキーは空、有料実行不可を明記）。
- 引き継ぎ3点セット（`AGENTS.md` / `docs/progress.md` / `docs/handover.md`）を整備。

### Step 1-2 の成果
- `requirements.txt`：実行・開発依存を範囲指定で固定。
- `Dockerfile`：Python 3.11-slim、非rootユーザー実行、`/health` ヘルスチェック付き。
- `compose.yaml`：`db`（PostgreSQL 16）/ `app`（FastAPI）/ `worker`（スケジューラ）を分離。DBは `127.0.0.1` のみに公開。
- `.dockerignore`：`.env`・`.venv`・`tests`・`docs` をビルドコンテキストから除外。
- `.venv` 構築完了（Python 3.11.15）。

### 検証
- 公開禁止情報チェック：ローカル絶対パス・実APIキー・個人IDを含まないことを確認。
- ツール実行確認：`uvicorn 0.54.0` / `alembic 1.20.1` / `pytest 8.4.2`。
- **未検証**：Docker CLI が本環境に無いため `compose.yaml` の実起動は未確認。Docker Desktop 導入環境での確認が必要。
- **未検証**：PostgreSQL 実接続（Docker導入後に `alembic upgrade head` で確認）。

### Step 2-5 の成果
- テスト用DBはテストごとの一時SQLiteファイル（`tmp_path`）とし、アプリのリクエストセッションとテストセッションを独立コネクションで扱う。
- Phase 1 の主要動作を68テストで固定：
  - RSS全件取得（15件へ切り詰めない）、HTTP失敗、利用条件未確認ソース停止
  - 正規化URL/GUID重複排除、別メディア同一イベントのクラスタリング、記事更新検知
  - JST日境界（0:00〜翌0:00直前）、公開日時不明記事の除外、RSS公開範囲外通知
  - 初回確認のはい/いいえ/キャンセル、確認前DB未保存、最大15件、表示件数、カテゴリOR、代表1件、重要度順/最新順
  - 手動更新、実行履歴API/画面、既読API、停止後補完取得、二重実行防止
  - SSRF拒否、URL正規化、HTML無害化、プロンプトインジェクション無害化、不正JSON 422、有料LLM有効化400

### Step 3-1 / 3-2 の成果
- Phase 2 は「RSS収集のみ」で日次計測を記録する。LLM呼び出し・記事本文保存は行わない。
- 日次計測値：RSS取得件数、重複排除後件数、重複率、カテゴリ別件数、RSS失敗率、平均タイトル文字数、平均概要文字数、推定入力トークン。
- 同一日の再計測は加算ではなく最新スナップショットで上書きする（RSS取得量は日中の累積合計ではないため）。
- レポート全体の `is_actual_measurement` は **2日分** の実測が揃うまで False。画面では「未計測」「暫定（1/2日）」「実測完了（2日分）」を表示する。
- 通常更新（`execute_digest_run`）と初回確認「はい」からも計測値を記録する。計測保存の失敗は警告に留め、ニュース処理の成功状態を壊さない。
- 画面・API：
  - `GET /metrics`：実測進捗、集計値、カテゴリ別件数、日次明細、「RSS取得量を計測」ボタン
  - `GET /api/metrics`：レポートJSON
  - `POST /api/metrics/measure`：RSSのみ計測して保存（ローカル/同一Wi-Fi限定）
- 検証結果：`pytest` 80 passed、`ruff check app tests` エラー0件。

### Step 4-1 〜 Step 5-1（Phase 3 & Phase 4 前半）の成果
- **LLM無料ポリシー二重ガード**：
  - `BaseLlmProvider.ensure_free_execution_allowed` と `model_router_service` の双方で検証。
  - 比較対象有料3モデル（`deepseek/deepseek-v4.1-flash`, `openai/gpt-6-luna`, `gemini-3.8-flash`）は `PAID_COMPARISON_MODEL_IDS` で常時遮断（`policy_violation`）。
  - Gemini は `gemini_free_tier_confirmed=True` かつ `GEMINI_FREE_TIER_ALLOWLIST` 一致モデルのみ実行可。
  - OpenRouter は `:free` または `openrouter/free` かつ料金0のモデルのみ実行可。
- **自動モデル切替・保留・再開（Step 4-2 / Step 5-1）**：
  - レート制限（429）・無料枠上限・認証エラー時は `model_catalog.availability_status` を更新し、次の無料モデルへ自動フォールバック（`switched_from_model` と `switch_reason` を `llm_usage_logs` に記録）。
  - 全無料モデル失敗時や1日呼び出し上限到達時は有料モデルへ絶対に切り替えず、RSS記事を保存・表示したまま `status="ai_pending"` として記録。
  - 回復後は `resume_pending_ai_run`（`POST /api/news/retry-pending` または基本画面の「保留中のAI処理を再開」ボタン）で未完了のAI分類・要約を再開可能。
- **トークン使用量集計・参考料金計算（Step 4-4 / Step 4-5）**：
  - `run_id` 単位で全APIリクエストの入力・出力・推論・合計トークンを合算。
  - 推論トークンが出力トークンに内包されている場合（`reasoning_included_in_output=True`）は二重加算しない。
  - トークン未計測（`None`）を `0` と混同せず `"未計測"` と表示し、価格未確認またはトークン未計測の比較モデルは `"算出不可"` と表示。
  - 極小の非ゼロ金額は `$0.00` とせず小数第6〜8位で表示。
  - カテゴリ変更時の再選別（`recategorize_from_saved_articles`）では既存分析を再利用し、新規トークン消費を `0` として過去実行のトークンを再計上しない。
- 検証結果：`pytest` **105 passed**、`ruff check app alembic tests` **エラー0件**。

### Step 5-2 / 5-3（Phase 4 完了）の成果
- **使用量ダッシュボード（Step 5-2）**：
  - `GET /usage`：当日API呼び出し上限、直近ログの合計トークン、モデル別集計、実行別集計、自動切替履歴、有料3モデル単価確認状態、直近ログを表示。
  - `GET /api/usage`：`limit_logs`（1〜500）、`limit_runs`（1〜50）、任意の `run_id` を受け付け、存在しない `run_id` は404を返す。
  - 未計測（`None`）は0ではなく「未計測」、一部未計測は「一部未計測」と表示。
  - 有料3モデルは参考単価のみを表示し、API呼び出しは行わない。未確認単価は「未確認（算出不可）」。
- **過去実行履歴（Step 5-2）**：run_id別のトークン（入力/出力/合計）、AI分類/要約件数、使用モデル、状態、分析再利用バッジを一覧表示。選択実行の詳細では未計測を明示。
- **テスト最終確認（Step 5-3）**：
  - `docs/test_matrix.md` に要件32項目とテストの対応を記録。**32/32項目 確認済み**。
  - `pytest`：**113 passed**。
  - `ruff check app alembic tests`：**All checks passed**。
- **README整備（Step 5-3）**：
  - 概要、主な機能、アーキテクチャ、無料LLMポリシー、PowerShell/cmd両方の仮想環境有効化、セットアップ、Docker/ローカル起動、画面/API一覧、テスト、セキュリティ、既知の制約を記載。
- **GitHub反映（Step 5-3）**：
  - Gitリポジトリを `main` ブランチで初期化。
  - 骨格、コア/ORM、スキーマ/Repository、RSS/計測、LLM、Web、テスト、ドキュメントの8コミットに分割。
  - 公開禁止情報スキャンを実施。検出された1件はログマスク用正規表現（`app/core/logging.py`）のみで、実際のローカルパス・APIキー・個人IDではないことを確認。
  - `gh` CLI（HTTPS認証）でpublicリポジトリを作成し、`main` をpush。

### 既知の注意点（要対応・任意）
- `.venv` は Python 3.11.15 で作成済みだが、作成に用いた `python` が PATH 上の別プロジェクトの venv を指していたため、`.venv/pyvenv.cfg` の `home` がその環境を参照している。パッケージは完全に独立しているが、参照元を削除すると `.venv` が壊れる。
- 対応案：Python 3.11 をシステムへ導入したうえで、プロジェクト直下で以下を実行して再作成する。
  ```powershell
  python -m venv .venv --clear
  .\.venv\Scripts\Activate.ps1
  pip install -r requirements.txt
  ```

---

## 4. 残タスク
- Docker Desktop導入後にPostgreSQL 16への接続と `alembic upgrade head` を確認する。
- Phase 2の2日分実測を確定するため、別日に通常更新または `POST /api/metrics/measure` を実行する。
- 実際のGemini / OpenRouter無料APIを設定し、無料枠・レート制限・自動切替の実挙動を確認する。
- 同一Wi-Fi内のスマートフォンから実機表示を確認する。
- 初期版は認証機能を持たないため、インターネットへ公開しない。公開する場合は認証・HTTPS・CSRF対策等を追加する。

---

## 5. 実行コマンド（Windows）

```powershell
# 仮想環境の作成・有効化（PowerShell）
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 起動・テスト・Lint
uvicorn app.main:app --reload
pytest
ruff check app alembic tests
```

```bat
:: コマンドプロンプト（cmd）
python -m venv .venv
.\.venv\Scripts\activate.bat
pip install -r requirements.txt

:: 起動・テスト・Lint
uvicorn app.main:app --reload
pytest
ruff check app alembic tests
```

```powershell
# DB（Docker Compose）
docker compose up -d
docker compose exec app alembic upgrade head
```

```powershell
# 公開禁止情報スキャン（プロジェクト外の一時スクリプトを使用）
# 検出対象：ローカル絶対パス、APIキー、GitHubトークン、秘密鍵、AWSキー
# 実際の認証情報は出力しない（マスク表示）
```

```powershell
# GitHub（HTTPS / gh CLI）
gh auth status
gh repo create dailyNewsDigest2 --public --source . --remote origin --push
```

---

## 変更履歴
- 2026-11-02: 初版作成（Step 1-1 完了時点）
- 2026-11-02: Step 2-5（Phase 1テスト）、Step 3-1（計測ロジック）、Step 3-2（計測画面/API）完了を反映
- 2026-11-03: Step 4-1〜4-5（Phase 3完了）および Step 5-1（既存分析再利用・保留再開）完了を反映
- 2026-11-03: Step 5-2（使用量ダッシュボード・履歴拡充）および Step 5-3（32項目テスト確認・README・GitHub反映）完了を反映
