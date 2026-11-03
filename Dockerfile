# =============================================================================
# dailyNewsDigest2 - Dockerfile
# -----------------------------------------------------------------------------
# 目的（Why）: アプリ実行環境を固定し、ローカルとDockerで同一動作を保証する。
# 仕様（What）:
#   - Python 3.11 スリムイメージを使用
#   - 依存パッケージを先にコピーしてレイヤキャッシュを活用
#   - 非rootユーザーで実行（セキュリティ対策）
# 入出力（I/O）: 8000/tcp で FastAPI を公開
# 注意（Caution）:
#   - APIキーはイメージに含めない（実行時に環境変数で注入する）
#   - .dockerignore で .env / .venv を除外する前提
# 変更履歴（Change Log）:
#   2026-11-02 - 初版作成（Step 1-2）
# =============================================================================

FROM python:3.11-slim

# --- 設定値 ---
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_HOME=/app \
    PORT=8000

WORKDIR ${APP_HOME}

# OS依存パッケージ（psycopg のビルドに必要な最小構成）
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# 依存関係を先にインストールしてキャッシュを効かせる
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# アプリケーションコード
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

# 非rootユーザーの作成と権限付与
RUN useradd --create-home --shell /bin/bash digest \
    && chown -R digest:digest ${APP_HOME}
USER digest

EXPOSE 8000

# ヘルスチェック（/health は app/main.py で実装する）
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:${PORT}/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
