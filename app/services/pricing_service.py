"""比較対象有料3モデルの参考料金計算・価格スナップショット管理サービス。

目的（Why）:
    実際に使用した無料LLMと同等のトークン量を、比較対象の有料3モデル
    （`deepseek/deepseek-v4.1-flash`, `openai/gpt-6-luna`, `gemini-3.8-flash`）で
    処理した場合の「1実行あたりの参考料金（USD）」を算出し、無料運用のコスト削減効果を可視化する
    （指示書 §16, §17.3, §17.4）。
仕様（What）:
    - `compute_single_model_reference_cost`: 推論トークン二重計上防止付きのUSD参考料金計算（純粋関数）
    - `upsert_price_snapshot`: 確認日時・情報源URL付きの単価スナップショット更新
    - `calculate_paid_model_estimates`: 1実行のトークン集計結果に対する有料3モデルの参考料金リスト生成
前提条件・依存関係（Assumption / Dependencies）:
    sqlalchemy>=2.0, `app.core.config`, `app.models.model_catalog`, `app.schemas.run_schema`,
    `app.services.model_catalog_service`, `app.services.usage_service`
入出力（I/O）:
    入力: SQLAlchemy `Session`, 合計入力・出力トークン数, 計測状態
    出力: `list[PaidModelCostEstimateSchema]`
注意点（Caution）:
    - 比較対象の有料3モデルは参考料金計算専用であり、API呼び出しを絶対に行わない（指示書 §11, §16.1）。
    - 単価未確認またはトークン未計測の場合は推測計算せず必ず `"算出不可"` とする（指示書 §16.2, §16.3）。
    - 極小金額を `"$0.00"` と誤表示しない（`format_usd_cost` で小数第6〜8位まで表示）。
今後の拡張ポイント（Future Work）:
    為替レート（USD/JPY）スナップショットを用いた日本円参考換算の併記。
変更履歴（Change Log）:
    2026-11-03 - 初版作成（Step 4-4）
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import COMPARISON_PAID_MODELS
from app.models.model_catalog import PriceSnapshot
from app.models.rss_source import utc_now
from app.schemas.run_schema import PaidModelCostEstimateSchema, format_usd_cost
from app.services.model_catalog_service import seed_default_models_and_prices
from app.services.rss_service import format_datetime_jst
from app.services.usage_service import (
    MEASUREMENT_STATE_PARTIAL,
    MEASUREMENT_STATE_UNMEASURED,
)

# =============================================================================
# 設定値ブロック（単価換算定数）
# =============================================================================

TOKENS_PER_MILLION: float = 1_000_000.0


def compute_single_model_reference_cost(
    input_tokens: int,
    output_tokens: int,
    input_price_per_1m: float,
    output_price_per_1m: float,
    reasoning_tokens: int | None = None,
    reasoning_included_in_output: bool = True,
) -> float:
    """1モデルあたりの参考料金（USD）を計算する（指示書 §16.3）。

    解説：
        基本計算式：
        `参考料金（USD） = (合計入力トークン数 ÷ 1,000,000) × 入力単価 + (合計出力トークン数 ÷ 1,000,000) × 出力単価`
        推論トークンが出力トークン数に内包されている場合（`reasoning_included_in_output=True`）は、
        二重課金計算にならないよう `output_tokens` のみに出力単価を掛ける。

    Args:
        input_tokens (int): 合計入力トークン数（0以上）。
        output_tokens (int): 合計出力トークン数（0以上）。
        input_price_per_1m (float): 100万入力トークンあたりUSD単価。
        output_price_per_1m (float): 100万出力トークンあたりUSD単価。
        reasoning_tokens (int | None): 推論トークン数（任意）。
        reasoning_included_in_output (bool): 推論トークンが出力トークンに内包されているか。

    Returns:
        float: 算出されたUSD参考料金。

    Side Effects:
        なし。

    Examples:
        >>> compute_single_model_reference_cost(1000, 500, 0.20, 0.80)
        0.0006
    """
    in_tok = max(0, int(input_tokens))
    out_tok = max(0, int(output_tokens))
    reas_tok = max(0, int(reasoning_tokens)) if reasoning_tokens is not None else 0

    effective_out_tok = (
        out_tok if reasoning_included_in_output else (out_tok + reas_tok)
    )

    in_cost = (in_tok / TOKENS_PER_MILLION) * float(input_price_per_1m)
    out_cost = (effective_out_tok / TOKENS_PER_MILLION) * float(output_price_per_1m)
    return round(in_cost + out_cost, 10)


def upsert_price_snapshot(
    session: Session,
    model_id: str,
    input_price_per_1m: float | None,
    output_price_per_1m: float | None,
    is_verified: bool = True,
    source_url: str | None = None,
    price_tier: str = "standard",
    model_name: str | None = None,
    provider: str | None = None,
) -> PriceSnapshot:
    """比較対象有料モデルの単価スナップショットを登録・更新する（指示書 §16.2）。

    Args:
        session (Session): SQLAlchemy セッション。
        model_id (str): 対象モデルID（例: `"openai/gpt-6-luna"`）。
        input_price_per_1m (float | None): 1M入力トークン単価（USD）。
        output_price_per_1m (float | None): 1M出力トークン単価（USD）。
        is_verified (bool): 公式価格として確認済みか。
        source_url (str | None): 価格確認元URL。
        price_tier (str): 価格区分（`"standard"` 等）。
        model_name (str | None): 表示名（省略時は設定定義または既存値）。
        provider (str | None): プロバイダー名。

    Returns:
        PriceSnapshot: 保存された価格スナップショット。

    Side Effects:
        `price_snapshots` テーブルへ INSERT / UPDATE し commit する。
    """
    seed_default_models_and_prices(session)
    stmt = select(PriceSnapshot).where(PriceSnapshot.model_id == model_id)
    snap = session.scalar(stmt)

    default_meta = next(
        (m for m in COMPARISON_PAID_MODELS if str(m["model_id"]) == model_id),
        None,
    )

    if snap is None:
        snap = PriceSnapshot(
            model_id=model_id,
            model_name=model_name
            or (str(default_meta["model_name"]) if default_meta else model_id),
            provider=provider
            or (str(default_meta["provider"]) if default_meta else "unknown"),
        )
        session.add(snap)
    else:
        if model_name:
            snap.model_name = model_name
        if provider:
            snap.provider = provider

    has_valid_prices = (
        input_price_per_1m is not None
        and output_price_per_1m is not None
        and float(input_price_per_1m) >= 0.0
        and float(output_price_per_1m) >= 0.0
    )
    verified_flag = bool(is_verified and has_valid_prices)

    snap.input_price_per_1m = (
        float(input_price_per_1m) if input_price_per_1m is not None else None
    )
    snap.output_price_per_1m = (
        float(output_price_per_1m) if output_price_per_1m is not None else None
    )
    snap.price_tier = str(price_tier or "standard")
    snap.is_verified = verified_flag
    snap.verified_at = utc_now() if verified_flag else None
    snap.source_url = source_url

    session.commit()
    session.refresh(snap)
    return snap


def format_unit_price(price: float | None) -> str:
    """1Mトークンあたりの単価を誤解のない表示文字列へ整形する。

    Args:
        price (float | None): 1MトークンあたりのUSD単価。

    Returns:
        str: `"$0.1400"` などの表示文字列。未確認時は `"未確認"`。

    Side Effects:
        なし。

    Examples:
        >>> format_unit_price(0.14)
        '$0.1400'
        >>> format_unit_price(None)
        '未確認'
    """
    if price is None:
        return "未確認"
    if price == 0.0:
        return "$0"
    if price < 0.000001:
        return f"${price:.8f}"
    if price < 0.01:
        return f"${price:.6f}"
    return f"${price:.4f}"


def get_price_snapshot_rows(session: Session) -> list[dict[str, Any]]:
    """比較対象有料3モデルの単価スナップショットをダッシュボード表示用に整形する（指示書 §16.2）。

    解説：
        - 単価が未確認（`is_verified=False` または `None`）の場合は推測値を出さず
          `status_label="未確認（算出不可）"` とする。
        - 確認済みの場合は確認日時と取得元URLを併記し、参考料金計算の根拠を追跡できるようにする。

    Args:
        session (Session): SQLAlchemy セッション。

    Returns:
        list[dict[str, Any]]: 3モデル分の単価・確認状態・確認日時・取得元URL。

    Side Effects:
        初期スナップショット未登録時に `seed_default_models_and_prices` を実行する。
    """
    seed_default_models_and_prices(session)
    rows: list[dict[str, Any]] = []

    for paid_def in COMPARISON_PAID_MODELS:
        mid = str(paid_def["model_id"])
        snap = session.scalar(
            select(PriceSnapshot).where(PriceSnapshot.model_id == mid)
        )
        if snap is None:
            rows.append(
                {
                    "model_id": mid,
                    "model_name": str(paid_def["model_name"]),
                    "provider": str(paid_def["provider"]),
                    "input_price_per_1m": None,
                    "output_price_per_1m": None,
                    "input_price_display": "未確認",
                    "output_price_display": "未確認",
                    "is_verified": False,
                    "status_label": "未確認（算出不可）",
                    "verified_at_str": "未確認",
                    "source_url": None,
                }
            )
            continue

        is_verified = bool(
            snap.is_verified
            and snap.input_price_per_1m is not None
            and snap.output_price_per_1m is not None
        )
        rows.append(
            {
                "model_id": mid,
                "model_name": snap.model_name or str(paid_def["model_name"]),
                "provider": snap.provider or str(paid_def["provider"]),
                "input_price_per_1m": snap.input_price_per_1m,
                "output_price_per_1m": snap.output_price_per_1m,
                "input_price_display": format_unit_price(snap.input_price_per_1m),
                "output_price_display": format_unit_price(snap.output_price_per_1m),
                "is_verified": is_verified,
                "status_label": "確認済み" if is_verified else "未確認（算出不可）",
                "verified_at_str": (
                    format_datetime_jst(snap.verified_at)
                    if is_verified and snap.verified_at
                    else "未確認"
                ),
                "source_url": snap.source_url,
            }
        )

    return rows


def calculate_paid_model_estimates(
    session: Session,
    total_input_tokens: int | None,
    total_output_tokens: int | None,
    measurement_state: str = "unused",
    total_reasoning_tokens: int | None = 0,
    reasoning_included_in_output: bool = True,
) -> list[PaidModelCostEstimateSchema]:
    """比較対象有料3モデルの参考料金リストを算出する（指示書 §16.1, §16.2, §16.3）。

    解説：
        1. 価格スナップショットが未確認（`is_verified == False` または単価 `None`）の場合は、
           推測で計算せず `display_cost="算出不可"`, `is_calculable=False` とする。
        2. トークン使用量が未計測（`measurement_state == "unmeasured"` またはトークン数 `None`）の場合も、
           0円と誤表示せず `display_cost="算出不可"`, `is_calculable=False` とする。
        3. 価格・トークンともに確認済みの場合は計算式を適用し、`format_usd_cost` で整形する。
           一部未計測がある場合（`partial_unmeasured`）は `"（計測分のみ）"` を明記する。

    Args:
        session (Session): SQLAlchemy セッション。
        total_input_tokens (int | None): 1実行の合計入力トークン数。
        total_output_tokens (int | None): 1実行の合計出力トークン数。
        measurement_state (str): `usage_service` が返した計測状態識別子。
        total_reasoning_tokens (int | None): 1実行の合計推論トークン数。
        reasoning_included_in_output (bool): 推論トークンが出力に内包されているか。

    Returns:
        list[PaidModelCostEstimateSchema]: 比較対象3モデルの参考料金スキーマリスト。

    Side Effects:
        初期スナップショット未登録時に `seed_default_models_and_prices` を実行する。
    """
    seed_default_models_and_prices(session)
    estimates: list[PaidModelCostEstimateSchema] = []

    for paid_def in COMPARISON_PAID_MODELS:
        mid = str(paid_def["model_id"])
        m_name = str(paid_def["model_name"])
        prov = str(paid_def["provider"])

        stmt = select(PriceSnapshot).where(PriceSnapshot.model_id == mid)
        snap = session.scalar(stmt)

        if (
            snap is None
            or not snap.is_verified
            or snap.input_price_per_1m is None
            or snap.output_price_per_1m is None
        ):
            estimates.append(
                PaidModelCostEstimateSchema(
                    model_id=mid,
                    model_name=m_name,
                    provider=prov,
                    input_price_per_1m=snap.input_price_per_1m if snap else None,
                    output_price_per_1m=snap.output_price_per_1m if snap else None,
                    estimated_cost_usd=None,
                    display_cost="算出不可",
                    is_calculable=False,
                    is_partial_estimate=False,
                    verified_at_str="価格未確認",
                    source_url=snap.source_url if snap else None,
                )
            )
            continue

        verified_str = (
            format_datetime_jst(snap.verified_at)
            if snap.verified_at
            else "確認済み"
        )

        if (
            measurement_state == MEASUREMENT_STATE_UNMEASURED
            or total_input_tokens is None
            or total_output_tokens is None
        ):
            estimates.append(
                PaidModelCostEstimateSchema(
                    model_id=mid,
                    model_name=m_name,
                    provider=prov,
                    input_price_per_1m=snap.input_price_per_1m,
                    output_price_per_1m=snap.output_price_per_1m,
                    estimated_cost_usd=None,
                    display_cost="算出不可",
                    is_calculable=False,
                    is_partial_estimate=False,
                    verified_at_str=verified_str,
                    source_url=snap.source_url,
                )
            )
            continue

        cost_usd = compute_single_model_reference_cost(
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            input_price_per_1m=snap.input_price_per_1m,
            output_price_per_1m=snap.output_price_per_1m,
            reasoning_tokens=total_reasoning_tokens,
            reasoning_included_in_output=reasoning_included_in_output,
        )
        base_str = format_usd_cost(cost_usd, is_verified=True)
        is_partial = measurement_state == MEASUREMENT_STATE_PARTIAL
        display_str = f"{base_str}（計測分のみ）" if is_partial else base_str

        estimates.append(
            PaidModelCostEstimateSchema(
                model_id=mid,
                model_name=m_name,
                provider=prov,
                input_price_per_1m=snap.input_price_per_1m,
                output_price_per_1m=snap.output_price_per_1m,
                estimated_cost_usd=cost_usd,
                display_cost=display_str,
                is_calculable=True,
                is_partial_estimate=is_partial,
                verified_at_str=verified_str,
                source_url=snap.source_url,
            )
        )

    return estimates


def validate_input(data: dict[str, Any]) -> dict[str, Any]:
    """料金計算サービスの入力辞書を検証する。"""
    if not isinstance(data, dict) or not isinstance(data.get("session"), Session):
        raise ValueError("有効な SQLAlchemy Session が必要です。")
    return dict(data)


def execute_logic(validated: dict[str, Any]) -> list[PaidModelCostEstimateSchema]:
    """有料3モデルの参考料金リストを計算する。"""
    return calculate_paid_model_estimates(
        session=validated["session"],
        total_input_tokens=validated.get("total_input_tokens", 0),
        total_output_tokens=validated.get("total_output_tokens", 0),
        measurement_state=str(validated.get("measurement_state", "measured")),
        total_reasoning_tokens=validated.get("total_reasoning_tokens", 0),
    )


def format_output(
    estimates: list[PaidModelCostEstimateSchema],
) -> list[dict[str, Any]]:
    """参考料金スキーマリストを辞書リストへ変換する。"""
    return [est.model_dump() for est in estimates]


def main(data: dict[str, Any]) -> list[dict[str, Any]]:
    """有料3モデルの参考料金計算・整形を一括実行する。"""
    validated = validate_input(data)
    estimates = execute_logic(validated)
    return format_output(estimates)
