# dailyNewsDigest2 — プロジェクト永続ルール（AGENTS.md）

## 概要
本プロジェクトは、国内外のRSSからニュースを自動収集し、ユーザーが選択したカテゴリの重要記事を最大15件選定・要約して表示する**個人向けWebアプリケーション**である。
OpenCode は本ファイルを参照して作業を行う。

- 対象要件定義書：`dailyNewsDigest 開発指示書 v3.0`
- 例外読み替え：Phase 2 の「7日間のRSS取得量計測」は **2日間** として扱う

## 最優先要件（要約）
1. 外部LLMのAPI利用料金を原則0円に維持する（無料利用確認済みモデルのみ実行）。
2. 正確で出典を追跡できるニュースを表示する。
3. PC・スマートフォンの両方で利用できる。
4. 毎朝7:00（Asia/Tokyo）に自動更新し、8:00までに閲覧可能とする。
5. ニュースカテゴリ・表示件数・LLMモデルをユーザーが選択できる。
6. Gemini API と OpenRouter の無料モデルを自動管理・切替する。
7. ニュース収集とLLM分析処理を完全に分離する。
8. 1実行単位（`run_id`）でトークン使用量と有料モデルの参考料金を表示する。
9. MVC（+ Service / Repository / Provider）に従った保守性の高い構成とする。
10. Python初心者が処理内容を理解できるコード・コメントを書く。

## 開発環境（venv標準）
- 仮想環境は venv を標準とする。他案件の conda env を流用しない。
- venv はプロジェクト直下の `.venv` に統一する（`python -m venv .venv` で作成）。
- `.venv/` と `venv/` は `.gitignore` で Git 除外する（絶対パスを含むため）。
- OS：Windows を前提とする。
- GPU：CUDA が利用可能な場合は使用する（利用不可の場合は CPU fallback）。
- データベース：PostgreSQL（Docker Compose）。テスト時は SQLite を許容する。

## README記載ルール
- README の「セットアップ」と「起動」には、PowerShell とコマンドプロンプト（cmd）の両方の仮想環境有効化手順を記載する。
- 各コマンドの対象シェルを明記し、PowerShell用とcmd用を混同させない。
- PowerShell：`.\.venv\Scripts\Activate.ps1`
- cmd：`.\.venv\Scripts\activate.bat`

## セキュリティ・公開禁止情報
以下の情報はコード・設定ファイル・ログ・コミットメッセージに絶対に含めないこと。

- ローカル絶対パス（ユーザーフォルダ配下のパス等）
- APIキー（Gemini / OpenRouter 等）
- GitHub 操作に必要な個人IDやトークン
- 認証情報全般

追加実装要件：
- `.env` によるAPIキー管理、`.env` を Git 管理対象外にする、`.env.example` を作成する。
- APIキーをブラウザへ送信しない、ログに記録しない（マスク処理必須）。
- 入力値検証、RSS取得先の制限、SSRF対策、HTML無害化、プロンプトインジェクション対策。
- APIタイムアウト、不正JSONへの対応。
- 初期版は個人利用・ローカルネットワーク利用を対象とし、インターネットへ無認証公開しない。
- スマートフォンは同一Wi-Fi内から閲覧できることを初期受入条件とする。

## LLM 無料API利用ポリシー（設定画面から無効化不可）
- **Gemini**：請求先未設定の Free Tier プロジェクトのみ使用。Free Tier と確認されたモデルのみ実行。
- **OpenRouter**：`:free` モデルおよび `openrouter/free` など無料と確認されたモデルのみ実行。
- **共通**：有料モデルへの自動切替は禁止。料金条件が不明なモデルは呼び出さない。無料利用条件が不明な場合は安全側で停止する。
- 有料3モデル（`deepseek/deepseek-v4.1-flash`, `openai/gpt-6-luna`, `gemini-3.8-flash`）は**参考料金の計算にのみ使用**し、API呼び出しを禁止する。
- モデル存在・無料枠・料金は恒久的な前提としない。公式情報で確認できない場合は「要確認」「算出不可」と表示する。

## GitHub 運用ルール
- GitHub を使用してバージョン管理を行う。
- コード生成・修正時は公開禁止情報が含まれていないか必ずチェックする。
- GitHub へ push する前に、禁止情報が含まれていない場合は「公開禁止情報は含まれていません」と表示すること。

### GitHub 認証・リポジトリ方式（共通定常ルール）
- リモートは **HTTPS方式**、認証は **gh CLI** を用いる。
- リポジトリは **public** で作成する（ポートフォリオにできるため）。
- 作成コマンド例：`gh repo create <name> --public --source . --remote origin`
- 認証情報（URL・トークン）はコードや AGENTS.md に直接記述しない。
- ブランチ：`main` を使用し、リモートで追跡する。

### コミット粒度（分割コミット）
- 成果は論理的な区切りごとに分割してコミットする（例：骨格/ロジック/API/フロント/テスト）。
- 1コミットにまとめず、`git log --oneline` で履歴が読める粒度にする。

### ハンドオーバー運用（サブステップ完了時・必須）
- 各サブステップ完了時に **`docs/progress.md`** を更新する。
- フェーズ完了時に **`docs/handover.md`** を更新する。
- 記載内容：方針（計画）→ 実装（実行）→ 結果 → 残タスク → 実行コマンド。
- これにより、人間とAIエージェントの双方がコンテキスト0%からでも再開できる。
- サブステップ完了報告時には、次のサブステップを実行する上での**おすすめモデルを理由付きで提示**する。

## 技術スタック
- Python 3.11
- FastAPI / Uvicorn
- SQLAlchemy 2.x / Alembic / psycopg
- Pydantic v2
- feedparser / httpx / Jinja2
- google-genai（Gemini API）
- APScheduler
- pytest / pytest-asyncio
- Docker Compose（PostgreSQL）
- HTML / CSS / Vanilla JavaScript

必要以上に技術やフレームワークを増やさないこと。

## ディレクトリ構造
```text
dailyNewsDigest2/
├── app/
│   ├── main.py
│   ├── core/          → 設定・DB・セキュリティ・ログ
│   ├── models/        → SQLAlchemy ORM
│   ├── schemas/       → Pydantic スキーマ
│   ├── repositories/  → DB永続化・トランザクション
│   ├── providers/     → LLM Adapter（Gemini / OpenRouter）
│   ├── services/      → ビジネスロジック
│   ├── controllers/   → HTTP受付・Service呼び出し
│   ├── views/         → Jinja2 テンプレート・CSS・JS
│   └── jobs/          → 定期実行・補完取得ワーカー
├── alembic/           → DBマイグレーション
├── tests/             → pytest
├── docs/              → progress.md / handover.md
├── .env.example
├── compose.yaml
└── README.md
```

## API 設計の原則
- Controller に RSS解析や LLM API 呼び出しを直接実装しない。
- Service から Provider（Adapter）を呼び出す。Provider は通信のみを担当する。
- Gemini と OpenRouter の出力は共通の Pydantic モデルで検証する。
- LLM 出力検証エラー時の再試行回数は制限する（無限リトライ禁止）。

## コーディング規約

### 1. ファイル先頭コメント（必須）
各ファイルの冒頭に以下をコメントとして記載すること。
- このファイルの目的（Why）
- 実装する機能の仕様（What）
- 前提条件・依存関係（Assumption / Dependencies）
- 入出力の定義（I/O）
- 注意点（Caution）
- 今後の拡張ポイント（Future Work）
- 変更履歴（Change Log）

※コメントは「読み手が5秒で概要を理解できる密度」で記載すること。

### 2. 設定値ブロック（コード冒頭）
- 設定値はコード冒頭にまとめること。
- 設定値は辞書または定数として定義すること。
- 設定値の意味をコメントで簡潔に説明すること。
- 設定値は `app/core/config.py` に集約すること。

### 3. 関数構成（コード本体）
以下の構造で関数を実装すること。
- `main()`：全体の流れを管理する
- `validate_input()`：入力チェック
- `execute_logic()`：ビジネスロジック
- `format_output()`：出力整形
- 必要に応じて utility 系関数を追加する

※関数は「単一責務」で設計すること。
※関数は副作用を避けること（I/O は上位層で行う）。
※型ヒント必須。

### 4. コード内コメント（適時）
- なぜこの処理が必要なのか（意図）
- なぜこの順番なのか（理由）
- なぜこのアルゴリズムを選んだのか（背景）
- 例外処理の理由

※コメントは「処理の意図」を中心に記載し、冗長な説明は避ける。

### 5. 出力形式
- 読みやすい構造化されたコード。
- Python の場合は PEP8 準拠（ruff で確認）。
- 関数ごとに docstring を付与すること。
  - 何をする関数なのかを一言で要約する（最初の1行）
  - 詳細説明を明確に書く
  - 引数・戻り値・例外を明確に書く
  - 副作用を必ず書く（Side Effects）
  - 使用例を書く（Examples）
  - docstring と型ヒントを矛盾させない
  - コメントは「なぜ」を書く。docstring は「何を」を書く
  - コードが複雑な場合、コメントで「解説：…」の形式で書く
  - 自動生成ツールで崩れない形式で書く（Sphinx/pdoc）
- ファイル生成時は既存構造を尊重すること。
- 例外処理は明示的に書くこと。

### 6. 初心者向け解説
- 専門用語は初出時に簡潔に説明する。
- 抽象論で終わらせず、実データ・変数の中身の変化で説明する。
- 「AとBは何が違うのか」を明確にする。

## ビルド・実行・テスト
- セットアップ：`python -m venv .venv`
- 依存導入：`.\.venv\Scripts\Activate.ps1` の後 `pip install -r requirements.txt`
- API起動：`uvicorn app.main:app --reload`
- テスト：`pytest`
- Lint：`ruff check .`
- DBマイグレーション：`alembic upgrade head`
- Docker：`docker compose up -d`

## DB 設計ルール
- 日時はタイムゾーン情報を保持して保存する（UTC保存、表示時に Asia/Tokyo 変換）。
- 適切な一意制約・外部キー・インデックスを設定する。
- Alembic でマイグレーションを管理する。
- 過去実行の結果が後日の更新で意図せず書き換わらないよう、`run_articles` は表示時点の分析結果を参照保持する。

## 注意点
- RAG は初期実装対象外（embedding 更新不要）。
- Excel 統合は初期実装対象外。
- AIエージェントのステート管理は `news_runs.status` で明確化する。
- RSS は「現在公開されている新着・更新記事全件」を対象とし、最初から15件に絞らない。
- 公開日時が不明な記事を対象日内と断定しない。
- 芸能・エンタメ・PR の除外はサイト単位ではなく記事単位で判定する。曖昧な記事は根拠なく除外せず「分類不能」とする。

## OpenCodeへの指示
- コード生成時はこの規約に従うこと。
- 新規ファイルは適切なディレクトリに配置すること。
- 修正時は差分を明確に示すこと。
- 作業は `docs/progress.md` のサブステップ単位で細切れに実施すること。
- 不明な外部仕様（API・料金・無料枠・RSS利用条件）は推測で実装しないこと。
