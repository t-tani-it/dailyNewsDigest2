"""無料モデルカタログ（model_catalog）および有料比較モデル料金（price_snapshots）モデル。

目的（Why）:
    1. Google AI Studio と OpenRouter のモデル情報・無料利用確認状態・利用可能状態を保持し、
       無料確認済み（`verified_free`）モデルのみ実行を許可する（指示書 §10, §11, §19）。
    2. 比較対象有料3モデルの単価を確認日時・情報源付きで保持し、
       未確認時は推測計算せず「算出不可」として扱う（指示書 §16）。
仕様（What）:
    - `ModelCatalog`: 無料利用判定フラグ `is_safe_free_model` プロパティ付きモデル管理テーブル
    - `PriceSnapshot`: 参考料金計算専用の単価スナップショット管理テーブル
前提条件・依存関係（Assumption / Dependencies）:
    sqlalchemy>=2.0, `app.core.database`, `app.models.rss_source`
入出力（I/O）:
    PostgreSQL / SQLite 上の `model_catalog` および `price_snapshots` テーブルレコード。
注意点（Caution）:
    `PriceSnapshot` に登録された有料モデルは参考料金計算専用であり、API呼び出し対象にしてはならない。
今後の拡張ポイント（Future Work）:
    価格改定履歴の複数世代保持。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 1-4）
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.rss_source import utc_now

# =============================================================================
# 設定値ブロック（無料確認ステータス・利用可能ステータス定数）
# =============================================================================

FREE_STATUS_VERIFIED: str = "verified_free"
FREE_STATUS_UNVERIFIED: str = "unverified"
FREE_STATUS_PAID_OR_CONDITIONAL: str = "paid_or_conditional"

AVAILABILITY_AVAILABLE: str = "available"
AVAILABILITY_RATE_LIMITED: str = "rate_limited"
AVAILABILITY_QUOTA_EXHAUSTED: str = "quota_exhausted"
AVAILABILITY_AUTH_ERROR: str = "auth_error"
AVAILABILITY_UNAVAILABLE: str = "unavailable"


class ModelCatalog(Base):
    """LLMモデルカタログ管理テーブル（`model_catalog`）。"""

    __tablename__ = "model_catalog"

    model_id: Mapped[str] = mapped_column(String(150), primary_key=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    model_name: Mapped[str] = mapped_column(String(200), nullable=False)
    free_tier_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=FREE_STATUS_UNVERIFIED
    )
    input_price_per_1m: Mapped[float | None] = mapped_column(Float, nullable=True)
    output_price_per_1m: Mapped[float | None] = mapped_column(Float, nullable=True)
    request_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    context_length: Mapped[int | None] = mapped_column(Integer, nullable=True)
    modalities: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    supports_structured_output: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    availability_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=AVAILABILITY_AVAILABLE
    )
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    source_info: Mapped[str] = mapped_column(String(255), nullable=False, default="manual")

    @property
    def is_executable_free_model(self) -> bool:
        """ニュース分析で実際にAPI呼び出ししてよい無料モデルかを判定する（指示書 §10, §11）。

        解説：
            1. `free_tier_status == "verified_free"` であること。
            2. 入出力・リクエスト料金が 0（または None で Gemini Free Tier 許可リスト一致）であること。
            3. `availability_status == "available"` であること。
            これらをすべて満たさないモデルは絶対に実行しない。

        Returns:
            bool: 安全に無料実行可能なモデルである場合のみ True。

        Side Effects:
            なし。

        Examples:
            >>> m = ModelCatalog(model_id="openrouter/free", provider="openrouter", model_name="Free", free_tier_status="verified_free", input_price_per_1m=0.0, output_price_per_1m=0.0, request_price=0.0, availability_status="available")
            >>> m.is_executable_free_model
            True
        """
        if self.free_tier_status != FREE_STATUS_VERIFIED:
            return False
        if self.availability_status != AVAILABILITY_AVAILABLE:
            return False
        if (self.input_price_per_1m or 0.0) > 0.0:
            return False
        if (self.output_price_per_1m or 0.0) > 0.0:
            return False
        return (self.request_price or 0.0) <= 0.0


class PriceSnapshot(Base):
    """比較対象有料モデルの参考料金スナップショットテーブル（`price_snapshots`）。"""

    __tablename__ = "price_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_id: Mapped[str] = mapped_column(String(150), unique=True, nullable=False, index=True)
    model_name: Mapped[str] = mapped_column(String(200), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    input_price_per_1m: Mapped[float | None] = mapped_column(Float, nullable=True)
    output_price_per_1m: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_tier: Mapped[str] = mapped_column(String(50), nullable=False, default="standard")
    is_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(500), nullable=True)


def validate_input(data: dict[str, Any]) -> dict[str, Any]:
    """モデルカタログ生成用の入力辞書を検証する。

    Args:
        data (dict[str, Any]): `model_id`, `provider`, `model_name` を含む辞書。

    Returns:
        dict[str, Any]: 検証済み辞書。

    Raises:
        ValueError: 必須項目が不足している場合。

    Side Effects:
        なし。

    Examples:
        >>> validate_input({"model_id": "openrouter/free", "provider": "openrouter", "model_name": "Free"})["provider"]
        'openrouter'
    """
    for key in ("model_id", "provider", "model_name"):
        if not data.get(key):
            raise ValueError(f"ModelCatalog 生成には {key} が必須です。")
    return dict(data)


def execute_logic(validated_data: dict[str, Any]) -> ModelCatalog:
    """検証済み辞書から `ModelCatalog` インスタンスを生成する。

    Args:
        validated_data (dict[str, Any]): `validate_input` 通過後の辞書。

    Returns:
        ModelCatalog: ORMモデルインスタンス。

    Side Effects:
        なし。

    Examples:
        >>> m = execute_logic({"model_id": "openai/gpt-6-luna", "provider": "openrouter", "model_name": "GPT-6 Luna", "input_price_per_1m": 0.1})
        >>> m.is_executable_free_model
        False
    """
    in_price = validated_data.get("input_price_per_1m")
    out_price = validated_data.get("output_price_per_1m")
    req_price = validated_data.get("request_price")

    # 料金が正の値である場合は、たとえ入力辞書で verified_free が指定されていても強制的に paid_or_conditional へ降格する
    requested_status = str(validated_data.get("free_tier_status", FREE_STATUS_UNVERIFIED))
    if any((p is not None and float(p) > 0.0) for p in (in_price, out_price, req_price)):
        safe_free_status = FREE_STATUS_PAID_OR_CONDITIONAL
    else:
        safe_free_status = requested_status

    return ModelCatalog(
        model_id=str(validated_data["model_id"]),
        provider=str(validated_data["provider"]),
        model_name=str(validated_data["model_name"]),
        free_tier_status=safe_free_status,
        input_price_per_1m=float(in_price) if in_price is not None else None,
        output_price_per_1m=float(out_price) if out_price is not None else None,
        request_price=float(req_price) if req_price is not None else None,
        context_length=validated_data.get("context_length"),
        modalities=list(validated_data.get("modalities") or ["text"]),
        supports_structured_output=bool(validated_data.get("supports_structured_output", False)),
        availability_status=str(
            validated_data.get("availability_status", AVAILABILITY_AVAILABLE)
        ),
        source_info=str(validated_data.get("source_info", "api")),
    )


def format_output(model_obj: ModelCatalog) -> dict[str, Any]:
    """`ModelCatalog` インスタンスを表示用辞書へ整形する。

    Args:
        model_obj (ModelCatalog): モデルカタログORMインスタンス。

    Returns:
        dict[str, Any]: 整形済み辞書。

    Side Effects:
        なし。

    Examples:
        >>> out = format_output(execute_logic({"model_id": "openrouter/free", "provider": "openrouter", "model_name": "Free", "free_tier_status": "verified_free"}))
        >>> out["is_executable_free_model"]
        True
    """
    return {
        "model_id": model_obj.model_id,
        "provider": model_obj.provider,
        "model_name": model_obj.model_name,
        "free_tier_status": model_obj.free_tier_status,
        "availability_status": model_obj.availability_status,
        "supports_structured_output": model_obj.supports_structured_output,
        "is_executable_free_model": model_obj.is_executable_free_model,
        "checked_at": model_obj.checked_at,
    }


def main(data: dict[str, Any]) -> dict[str, Any]:
    """入力検証・モデル生成・辞書整形の一連の処理を実行する。

    Args:
        data (dict[str, Any]): モデルカタログ入力データ。

    Returns:
        dict[str, Any]: 整形済み辞書。

    Side Effects:
        なし。

    Examples:
        >>> main({"model_id": "openrouter/free", "provider": "openrouter", "model_name": "Free"})["free_tier_status"]
        'unverified'
    """
    validated = validate_input(data)
    instance = execute_logic(validated)
    return format_output(instance)
