"""API利用履歴（llm_usage_logs）のSQLAlchemy ORMモデル。

目的（Why）:
    1実行（`run_id`）内で発生したすべてのLLM API呼び出しのトークン使用量、
    推論トークン内訳、計測状態、エラー、自動切替理由を記録する（指示書 §14, §15, §19）。
仕様（What）:
    `LlmUsageLog` テーブル定義と推論トークン二重計上防止ロジック。
前提条件・依存関係（Assumption / Dependencies）:
    sqlalchemy>=2.0, `app.core.database`, `app.models.rss_source`
入出力（I/O）:
    PostgreSQL / SQLite 上の `llm_usage_logs` テーブルレコード。
注意点（Caution）:
    - 使用量不明を 0 として扱わず、`None` かつ `usage_status="unmeasured"` として保存すること（指示書 §15.1）。
    - 推論トークンが出力トークン数に内包されている場合は二重計上しないこと。
今後の拡張ポイント（Future Work）:
    レスポンスレイテンシ（ミリ秒）の記録。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 1-4）
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.rss_source import utc_now

# =============================================================================
# 設定値ブロック（API呼び出し用途・計測状態定数）
# =============================================================================

USAGE_STATUS_MEASURED: str = "measured"
USAGE_STATUS_PARTIAL: str = "partial_unmeasured"
USAGE_STATUS_UNMEASURED: str = "unmeasured"
USAGE_STATUS_FAILED: str = "failed"


def generate_call_id() -> str:
    """API呼び出しごとの一意ID（UUID4文字列）を生成する。

    Returns:
        str: 36文字のUUID4文字列。

    Side Effects:
        なし。

    Examples:
        >>> len(generate_call_id())
        36
    """
    return str(uuid.uuid4())


class LlmUsageLog(Base):
    """LLM API利用履歴テーブル（`llm_usage_logs`）。"""

    __tablename__ = "llm_usage_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("news_runs.run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    call_id: Mapped[str] = mapped_column(
        String(36), unique=True, nullable=False, default=generate_call_id
    )
    purpose: Mapped[str] = mapped_column(String(30), nullable=False, default="classify")
    provider: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    model_id: Mapped[str] = mapped_column(String(150), nullable=False, index=True)
    called_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, index=True
    )
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reasoning_included_in_output: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    usage_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=USAGE_STATUS_MEASURED
    )
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    switched_from_model: Mapped[str | None] = mapped_column(String(150), nullable=True)
    switch_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    target_article_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


def validate_input(data: dict[str, Any]) -> dict[str, Any]:
    """API利用履歴生成用の入力辞書を検証する。

    Args:
        data (dict[str, Any]): `run_id`, `provider`, `model_id` を含む辞書。

    Returns:
        dict[str, Any]: 検証済み辞書。

    Raises:
        ValueError: 必須項目が欠落している場合。

    Side Effects:
        なし。

    Examples:
        >>> validate_input({"run_id": "r1", "provider": "gemini", "model_id": "gemini-2.0-flash"})["run_id"]
        'r1'
    """
    for key in ("run_id", "provider", "model_id"):
        if not data.get(key):
            raise ValueError(f"LlmUsageLog 生成には {key} が必須です。")
    return dict(data)


def execute_logic(validated_data: dict[str, Any]) -> LlmUsageLog:
    """検証済み辞書から `LlmUsageLog` インスタンスを生成する（二重計上防止付き）。

    解説：
        - 入力または出力トークン数が `None`（APIから返却されなかった場合）は、
          0 として扱わず `usage_status="unmeasured"` として記録する（指示書 §15.1）。
        - `reasoning_included_in_output=True` の場合、`total_tokens` が未指定なら
          `input_tokens + output_tokens` のみを合算し、`reasoning_tokens` を二重加算しない。

    Args:
        validated_data (dict[str, Any]): `validate_input` 通過後の辞書。

    Returns:
        LlmUsageLog: ORMモデルインスタンス。

    Side Effects:
        なし。

    Examples:
        >>> log = execute_logic({"run_id": "r1", "provider": "openrouter", "model_id": "openrouter/free", "input_tokens": 100, "output_tokens": 50, "reasoning_tokens": 20})
        >>> log.total_tokens
        150
    """
    in_tok = validated_data.get("input_tokens")
    out_tok = validated_data.get("output_tokens")
    reas_tok = validated_data.get("reasoning_tokens")
    tot_tok = validated_data.get("total_tokens")
    included = bool(validated_data.get("reasoning_included_in_output", True))

    if in_tok is not None and out_tok is not None:
        in_val = int(in_tok)
        out_val = int(out_tok)
        if tot_tok is not None:
            total_val: int | None = int(tot_tok)
        elif not included and reas_tok is not None:
            total_val = in_val + out_val + int(reas_tok)
        else:
            total_val = in_val + out_val
        status = str(validated_data.get("usage_status", USAGE_STATUS_MEASURED))
    else:
        in_val = int(in_tok) if in_tok is not None else None
        out_val = int(out_tok) if out_tok is not None else None
        total_val = int(tot_tok) if tot_tok is not None else None
        status = str(validated_data.get("usage_status", USAGE_STATUS_UNMEASURED))

    return LlmUsageLog(
        run_id=str(validated_data["run_id"]),
        call_id=str(validated_data.get("call_id") or generate_call_id()),
        purpose=str(validated_data.get("purpose", "classify")),
        provider=str(validated_data["provider"]),
        model_id=str(validated_data["model_id"]),
        input_tokens=in_val,
        output_tokens=out_val,
        reasoning_tokens=int(reas_tok) if reas_tok is not None else None,
        total_tokens=total_val,
        reasoning_included_in_output=included,
        usage_status=status,
        http_status=validated_data.get("http_status"),
        error_message=validated_data.get("error_message"),
        switched_from_model=validated_data.get("switched_from_model"),
        switch_reason=validated_data.get("switch_reason"),
        target_article_count=int(validated_data.get("target_article_count", 0)),
    )


def format_output(log_obj: LlmUsageLog) -> dict[str, Any]:
    """`LlmUsageLog` インスタンスを辞書へ整形する。

    Args:
        log_obj (LlmUsageLog): 利用履歴ORMインスタンス。

    Returns:
        dict[str, Any]: 整形済み辞書。

    Side Effects:
        なし。

    Examples:
        >>> out = format_output(execute_logic({"run_id": "r1", "provider": "gemini", "model_id": "gemini-2.0-flash"}))
        >>> out["usage_status"]
        'unmeasured'
    """
    return {
        "call_id": log_obj.call_id,
        "run_id": log_obj.run_id,
        "purpose": log_obj.purpose,
        "provider": log_obj.provider,
        "model_id": log_obj.model_id,
        "input_tokens": log_obj.input_tokens,
        "output_tokens": log_obj.output_tokens,
        "reasoning_tokens": log_obj.reasoning_tokens,
        "total_tokens": log_obj.total_tokens,
        "usage_status": log_obj.usage_status,
        "http_status": log_obj.http_status,
        "error_message": log_obj.error_message,
        "switched_from_model": log_obj.switched_from_model,
        "switch_reason": log_obj.switch_reason,
        "target_article_count": log_obj.target_article_count,
    }


def main(data: dict[str, Any]) -> dict[str, Any]:
    """入力検証・利用履歴生成・辞書整形の一連の処理を実行する。

    Args:
        data (dict[str, Any]): 利用履歴入力データ。

    Returns:
        dict[str, Any]: 整形済み辞書。

    Side Effects:
        なし。

    Examples:
        >>> main({"run_id": "r1", "provider": "gemini", "model_id": "gemini-2.0-flash", "input_tokens": 10, "output_tokens": 5})["total_tokens"]
        15
    """
    validated = validate_input(data)
    instance = execute_logic(validated)
    return format_output(instance)
