# dailyNewsDigest2

国内外のRSSからニュースを自動収集し、ユーザーが選んだカテゴリの重要記事を最大15件選定・要約して表示する、個人向けのニュースダイジェストWebアプリです。

ChatGPT Plus などの情報収集機能の代替として、**外部LLMのAPI利用料金を0円に維持する**ことを最優先に設計しています。

- 対象要件定義書：`dailyNewsDigest 開発指示書 v3.0`
- Phase 2 の「7日間のRSS取得量計測」は、本プロジェクトでは **2日間** として扱います。

---

## 1. 主な機能

- RSSの新着・更新記事を全件収集（最初から15件に切り詰めない）
- URL・GUIDの重複排除、別メディアの同一イベント記事のクラスタリング
- JST（Asia/Tokyo）の日境界による対象日判定
- 初回起動確認（はい / いいえ / キャンセル）
- カテゴリ・表示件数・LLMモデルのユーザー選択
- Python一次フィルタ + 無料LLMによるカテゴリ分類・重要度判定・日本語要約
- 毎朝 07:00（Asia/Tokyo）の自動更新、二重実行防止、停止後の補完取得
- `run_id` 単位のトークン使用量集計と、有料3モデルの参考料金表示
- RSS取得量の日次計測（Phase 2、2日間）
- 過去実行履歴、AI使用量ダッシュボード
- PC・スマートフォン（同一Wi-Fi内）対応のレスポンシブ画面

---

## 2. アーキテクチャ

MVC + Service + Repository + Provider（Adapter）構成です。

```text
Controller  → HTTP受付・入力検証・Service呼び出し
Service     → ビジネスロジック（収集・選定・AI処理・集計）
Repository  → DB永続化・トランザクション
Provider    → 外部API通信（Gemini / OpenRouter）
Model       → SQLAlchemy ORM
View        → Jinja2テンプレート / CSS / Vanilla JavaScript
```

Controller にRSS解析やLLM SDK呼び出しを直接実装しません。RSS収集とLLM分析は完全に分離しており、LLMが利用できない場合でもRSS収集・記事閲覧は継続します。

### ディレクトリ構成

```text
dailyNewsDigest2/
├── app/
│   ├── main.py
│   ├── core/          → 設定・DB・セキュリティ・ログ
│   ├── models/        → SQLAlchemy ORM（全10テーブル）
│   ├── schemas/       → Pydanticスキーマ
│   ├── repositories/  → DB永続化
│   ├── providers/     → LLM Adapter（Gemini / OpenRouter）
│   ├── services/      → ビジネスロジック
│   ├── controllers/   → HTML / JSON API
│   ├── views/         → Jinja2・CSS・JavaScript
│   └── jobs/          → 定期実行ワーカー
├── alembic/           → DBマイグレーション
├── tests/             → pytest
├── docs/              → progress.md / handover.md / test_matrix.md
├── .env.example
├── compose.yaml
└── README.md
```

---

## 3. 無料LLMポリシー（重要）

本アプリは、**無料利用が確認済みのモデルのみ**を実行します。設定画面から無効化することはできません。

- Gemini：請求先未設定の Free Tier プロジェクトで、許可リストに一致するモデルのみ実行
- OpenRouter：`:free` モデルまたは `openrouter/free` で、料金が0と確認できるモデルのみ実行
- 料金条件が不明なモデルは呼び出さない
- 有料モデルへの自動切替は禁止
- 全無料モデルが利用不能な場合は、有料モデルへ切り替えず `ai_pending` で安全に停止

### 参考料金の扱い

以下の有料3モデルは**参考料金の計算にのみ使用**し、API呼び出しは行いません。

- `deepseek/deepseek-v4.1-flash`
- `openai/gpt-6-luna`
- `gemini-3.8-flash`

単価が未確認、またはトークンが未計測の場合は、推測計算せず「算出不可」と表示します。極小の非ゼロ金額を `$0.00` と誤表示しないよう、小数第6〜8位まで表示します。

---

## 4. 動作環境

- Windows
- Python 3.11
- PostgreSQL 16（Docker Compose）
- Docker Desktop（PostgreSQL起動用）
- テスト時はSQLiteを使用（PostgreSQL不要）

GPUは必須ではありません。本アプリのLLM処理は外部APIを利用するため、ローカルGPUは使用しません。

---

## 5. セットアップ

### 5.1 仮想環境の作成

PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

コマンドプロンプト（cmd）：

```bat
python -m venv .venv
.\.venv\Scripts\activate.bat
pip install -r requirements.txt
```

### 5.2 環境変数ファイルの作成

PowerShell：

```powershell
Copy-Item .env.example .env
```

コマンドプロンプト（cmd）：

```bat
copy .env.example .env
```

`.env` はGit管理対象外です。実際のAPIキーは `.env` にのみ記入し、コード・README・コミットメッセージには記載しないでください。

### 5.3 APIキーと無料枠の設定

`.env` を編集します。

```dotenv
# Gemini（Google AI Studio / Free Tierのみ）
GEMINI_API_KEY=（実際のキー）
GEMINI_ENABLED=true
GEMINI_FREE_TIER_CONFIRMED=true

# OpenRouter（無料モデルのみ）
OPENROUTER_API_KEY=（実際のキー）
OPENROUTER_ENABLED=true

# LLM機能全体
LLM_ENABLED=true
```

- `GEMINI_FREE_TIER_CONFIRMED=true` は、Google AI Studio側で**請求先未設定（Free Tier）**であることを確認した場合のみ設定してください。
- APIキーが未設定、または無料確認ができない場合、LLM処理は安全側で停止します。
- `ALLOW_PAID_LLM_EXECUTION` はコード上の定数で `False` に固定されており、環境変数や設定画面から変更できません。

---

## 6. 起動

### 6.1 PostgreSQLとアプリをDocker Composeで起動

```powershell
docker compose up -d
```

初回はマイグレーションを実行します。

```powershell
docker compose exec app alembic upgrade head
```

ブラウザで次を開きます。

```text
http://localhost:8000/
```

スマートフォンからは、PCの同一Wi-Fi内IPアドレスを使って次を開きます。

```text
http://（PCのIPアドレス）:8000/
```

> 初期版は個人利用・ローカルネットワーク利用を前提としています。インターネットへ無認証で公開しないでください。

### 6.2 ローカル（venv）で起動

PostgreSQLをDockerで起動したうえで、アプリをローカル実行します。

PowerShell：

```powershell
.\.venv\Scripts\Activate.ps1
alembic upgrade head
uvicorn app.main:app --reload
```

コマンドプロンプト（cmd）：

```bat
.\.venv\Scripts\activate.bat
alembic upgrade head
uvicorn app.main:app --reload
```

### 6.3 定期実行ワーカー

Docker Composeでは `worker` サービスが起動します。ローカルで個別に起動する場合は次を実行します。

```powershell
.\.venv\Scripts\Activate.ps1
python -m app.jobs.scheduler_worker
```

定期実行は毎朝 07:00（Asia/Tokyo）です。二重実行はDBロックで防止します。

---

## 7. 画面一覧

| パス | 内容 |
|---|---|
| `/` | 重要ニュース一覧、LLMモデル選択、手動更新、保留AI処理の再開 |
| `/onboarding` | 初回起動確認（はい / いいえ / キャンセル） |
| `/history` | 過去実行履歴、選定記事、実行別トークン・参考料金 |
| `/usage` | AI使用量ダッシュボード（モデル別・実行別・切替履歴・参考単価） |
| `/metrics` | RSS取得量の日次計測レポート（Phase 2、2日間） |
| `/settings` | 表示件数、カテゴリ、自動更新時刻、RSS取得元、LLMモデル |
| `/health` | ヘルスチェック、DB接続状態、有料LLM実行可否 |

---

## 8. API一覧

| メソッド | パス | 内容 |
|---|---|---|
| GET | `/api/runs` | 実行履歴一覧 |
| GET | `/api/runs/{run_id}` | 指定実行の記事・サマリー |
| POST | `/api/onboarding/preview` | 初回確認用プレビュー（DB未保存） |
| POST | `/api/onboarding/decision` | 初回確認のはい / いいえ / キャンセル |
| POST | `/api/news/refresh` | 手動ニュース更新 |
| POST | `/api/news/recategorize` | 保存済み記事から再選別（既存分析を再利用） |
| POST | `/api/news/retry-pending` | `ai_pending` のAI処理を再開 |
| POST | `/api/articles/{article_id}/read` | 既読 / 未読の切替 |
| GET | `/api/settings` | 設定取得（APIキー値は返さない） |
| PUT | `/api/settings` | 設定更新 |
| GET | `/api/rss-sources` | RSS取得元一覧 |
| POST | `/api/rss-sources` | RSS取得元追加（SSRF検証付き） |
| PATCH | `/api/rss-sources/{source_id}` | RSS取得元の有効化・設定変更 |
| GET | `/api/models` | 無料モデル一覧 |
| POST | `/api/models/refresh` | モデル一覧・価格スナップショット更新 |
| GET | `/api/metrics` | RSS取得量レポート |
| POST | `/api/metrics/measure` | RSS取得量のみ計測（LLMなし・記事保存なし） |
| GET | `/api/usage` | AI使用量ダッシュボード（`limit_logs` / `limit_runs` / `run_id`） |

`/api/settings` の更新、RSS取得元の追加・変更、手動更新、再選別、保留AI再開、計測実行などの操作系APIは、ローカルまたは同一Wi-Fi内からのアクセスに限定しています。

---

## 9. テストとLint

### 9.1 全テスト

PowerShell：

```powershell
.\.venv\Scripts\Activate.ps1
pytest
```

コマンドプロンプト（cmd）：

```bat
.\.venv\Scripts\activate.bat
pytest
```

### 9.2 Lint

```powershell
.\.venv\Scripts\Activate.ps1
ruff check app alembic tests
```

### 9.3 テスト確認表

要件32項目とテストの対応は [`docs/test_matrix.md`](docs/test_matrix.md) を参照してください。

2026-11-03 時点の結果：

- `pytest`：**113 passed**
- `ruff check app alembic tests`：**All checks passed**

> テストはテストごとに独立した一時SQLiteファイルDBを使用します。本番用PostgreSQLには接続しません。

---

## 10. セキュリティ

- APIキーは `.env` のみで管理し、Git管理対象外にする
- APIキーをブラウザへ送信しない
- ログ出力時にAPIキー・ローカルパス・トークンをマスクする
- RSS取得元URLのSSRF検証（プライベートIP・不正スキームの拒否）
- HTMLタグの無害化
- LLMプロンプトインジェクション対策
- LLM出力JSONのスキーマ検証と回数制限付き再試行
- APIタイムアウト設定
- 操作系APIをローカル / 同一Wi-Fi内に制限する

公開禁止情報（ローカル絶対パス、APIキー、個人ID、認証情報）は、コード・設定ファイル・ログ・コミットメッセージに含めないでください。

---

## 11. 既知の制約・未検証事項

- Docker CLIが開発環境に無かったため、`compose.yaml` の実起動とPostgreSQL 16への実接続は未検証です。Docker Desktop導入後に確認してください。PostgreSQL 18の一時クラスタでは`alembic upgrade head`と主要画面の動作を確認済みです。
- 実際のGemini / OpenRouter無料APIは、`.env`にAPIキーを設定するまで未検証です。OpenRouterの公開モデル一覧とGemini APIの到達性は確認済みです。
- 同一Wi-Fi内の実機スマートフォンからの表示は未確認です。PCのLAN IP経由では主要画面がHTTP 200でした。Windows Firewallの受信許可（TCP 8000、同一サブネット限定）が必要な場合があります。
- Phase 2の実測は実行した日のみ記録されます。2日分の実測を確定するには、別日に通常更新または `POST /api/metrics/measure` を実行してください。
- RSS取得元の利用条件は、初期候補について確認済みの状態で登録していますが、運用前に各サイトの最新の利用規約を確認してください。
- モデルの存在・無料枠・料金は恒久的な前提ではありません。公式情報で確認できない場合は「要確認」「算出不可」と表示します。
- 初期版は個人利用・ローカルネットワーク利用を前提とし、認証機能を持ちません。インターネットへ公開しないでください。

---

## 12. ドキュメント

- [`AGENTS.md`](AGENTS.md)：プロジェクト永続ルール、コーディング規約、無料LLMポリシー
- [`docs/progress.md`](docs/progress.md)：サブステップの進捗と完了記録
- [`docs/handover.md`](docs/handover.md)：設計判断、実装結果、残タスク、実行コマンド
- [`docs/test_matrix.md`](docs/test_matrix.md)：要件32項目とテストの対応表

---

## 13. ライセンス

個人利用を目的としたプロジェクトです。外部公開・再配布を行う場合は、利用するRSS・API・モデルの各利用規約を確認してください。
