"""1実行単位のAI処理・使用量サマリーおよび有料3モデル参考料金スキーマ。

目的（Why）:
    ニュースカード単位ではなく「1実行単位（`run_id`）」でトークン使用量と
    有料3モデルの参考料金を集計・表示するための共通データ構造を定義する（指示書 §14, §15, §16, §17.3）。
仕様（What）:
    - `PaidModelCostEstimateSchema`: 比較対象1モデルあたりの参考料金と確認日時
    - `RunUsageSummarySchema`: 1実行の実行情報・AI処理情報・有料3モデル参考料金・必須注意書き
前提条件・依存関係（Assumption / Dependencies）:
    pydantic>=2.7
入出力（I/O）:
    Service 層から Controller / View へ受け渡す実行サマリーモデル。
注意点（Caution）:
    - 価格情報またはトークン使用量が未確認の場合は `"算出不可"` とする（指示書 §16.3）。
    - 小額料金を `"$0.00"` と誤表示しないこと。
今後の拡張ポイント（Future Work）:
    キャッシュトークン割引の参考試算欄の追加。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 2-1）
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

# =============================================================================
# 設定値ブロック（指示書 §17.3 必須注意書き文言）
# =============================================================================

REFERENCE_PRICE_DISCLAIMER: str = (
    "表示料金は、同じトークン数を使用したと仮定した参考値です。"
    "実際に有料モデルを使用・課金した金額ではありません。"
)


class PaidModelCostEstimateSchema(BaseModel):
    """比較対象有料モデル1件分の参考料金表示スキーマ（指示書 §16）。"""

    model_id: str
    model_name: str
    provider: str
    input_price_per_1m: float | None = None
    output_price_per_1m: float | None = None
    estimated_cost_usd: float | None = None
    display_cost: str = "算出不可"
    is_calculable: bool = False
    is_partial_estimate: bool = False
    verified_at_str: str = "未確認"
    source_url: str | None = None


class RunUsageSummarySchema(BaseModel):
    """画面上部・下部に1つだけ表示する「AI処理・使用量サマリー」スキーマ（指示書 §17.3, §17.5）。"""

    run_id: str
    parent_run_id: str | None = None
    executed_at_jst_str: str
    run_type: str
    run_type_label: str
    fetched_count: int = 0
    deduped_count: int = 0
    ai_classified_count: int = 0
    ai_summarized_count: int = 0
    displayed_count: int = 0
    status: str = "completed"
    status_label: str = "完了"
    reused_analysis: bool = False

    # AI処理情報
    used_provider: str = "未使用（RSS収集のみ）"
    used_model: str = "未使用"
    api_request_count: int = 0
    total_input_tokens: int | None = 0
    total_output_tokens: int | None = 0
    total_reasoning_tokens: int | None = 0
    total_tokens: int | None = 0
    usage_measurement_status: str = "LLM未使用"

    # 有料3モデルの参考料金
    paid_model_estimates: list[PaidModelCostEstimateSchema] = Field(default_factory=list)
    disclaimer: str = REFERENCE_PRICE_DISCLAIMER


RUN_TYPE_LABELS: dict[str, str] = {
    "initial": "初回取得",
    "manual": "手動更新",
    "scheduled": "定期更新",
    "recategorize": "カテゴリ変更による再選別",
    "retry": "失敗した処理の再実行",
    "catchup": "停止後の補完取得",
}

RUN_STATUS_LABELS: dict[str, str] = {
    "running": "実行中",
    "completed": "完了",
    "ai_pending": "AI処理保留（RSS閲覧可）",
    "failed": "エラー終了",
    "preview": "初回確認プレビュー",
}


def format_usd_cost(cost_usd: float | None, is_verified: bool = True) -> str:
    """USD金額を誤解のない文字列表現へ整形する（小額の $0.00 誤表示防止：指示書 §16.3）。

    Args:
        cost_usd (float | None): 計算されたUSD参考料金。
        is_verified (bool): 単価およびトークン数が確認済みか。

    Returns:
        str: 表示用文字列（例: `"$0.004200"`, `"$0.000015"`, `"$0"`, `"算出不可"`）。

    Side Effects:
        なし。

    Examples:
        >>> format_usd_cost(None, is_verified=False)
        '算出不可'
        >>> format_usd_cost(0.0, is_verified=True)
        '$0'
        >>> format_usd_cost(0.00045, is_verified=True)
        '$0.000450'
    """
    if not is_verified or cost_usd is None:
        return "算出不可"
    if cost_usd == 0.0:
        return "$0"
    if cost_usd < 0.000001:
        return f"${cost_usd:.8f}"
    if cost_usd < 0.01:
        return f"${cost_usd:.6f}"
    return f"${cost_usd:.4f}"


def validate_input(data: dict[str, Any]) -> dict[str, Any]:
    """サマリー生成用の入力辞書を検証する。

    Args:
        data (dict[str, Any]): `run_id`, `run_type` 等を含む辞書。

    Returns:
        dict[str, Any]: 検証済み辞書。

    Raises:
        ValueError: `run_id` が無い場合。

    Side Effects:
        なし。

    Examples:
        >>> validate_input({"run_id": "r-1"})["run_id"]
        'r-1'
    """
    if not data.get("run_id"):
        raise ValueError("RunUsageSummarySchema には run_id が必須です。")
    return dict(data)


def execute_logic(validated_data: dict[str, Any]) -> RunUsageSummarySchema:
    """検証済み辞書から `RunUsageSummarySchema` インスタンスを生成する。

    Args:
        validated_data (dict[str, Any]): `validate_input` 通過後の辞書。

    Returns:
        RunUsageSummarySchema: 検証済みサマリースキーマ。

    Side Effects:
        なし。

    Examples:
        >>> s = execute_logic({"run_id": "r-1", "run_type": "scheduled"})
        >>> s.run_type_label
        '定期更新'
    """
    r_type = str(validated_data.get("run_type", "manual"))
    status = str(validated_data.get("status", "completed"))
    return RunUsageSummarySchema(
        run_id=str(validated_data["run_id"]),
        parent_run_id=validated_data.get("parent_run_id"),
        executed_at_jst_str=str(
            validated_data.get("executed_at_jst_str")
            or datetime.now().strftime("%Y/%m/%d %H:%M")
        ),
        run_type=r_type,
        run_type_label=RUN_TYPE_LABELS.get(r_type, r_type),
        fetched_count=int(validated_data.get("fetched_count", 0)),
        deduped_count=int(validated_data.get("deduped_count", 0)),
        ai_classified_count=int(validated_data.get("ai_classified_count", 0)),
        ai_summarized_count=int(validated_data.get("ai_summarized_count", 0)),
        displayed_count=int(validated_data.get("displayed_count", 0)),
        status=status,
        status_label=RUN_STATUS_LABELS.get(status, status),
        reused_analysis=bool(validated_data.get("reused_analysis", False)),
        used_provider=str(validated_data.get("used_provider") or "未使用（RSS収集のみ）"),
        used_model=str(validated_data.get("used_model") or "未使用"),
        api_request_count=int(validated_data.get("api_request_count", 0)),
        total_input_tokens=validated_data.get("total_input_tokens", 0),
        total_output_tokens=validated_data.get("total_output_tokens", 0),
        total_reasoning_tokens=validated_data.get("total_reasoning_tokens", 0),
        total_tokens=validated_data.get("total_tokens", 0),
        usage_measurement_status=str(
            validated_data.get("usage_measurement_status", "LLM未使用")
        ),
        paid_model_estimates=list(validated_data.get("paid_model_estimates") or []),
    )


def format_output(summary: RunUsageSummarySchema) -> dict[str, Any]:
    """`RunUsageSummarySchema` を辞書へ変換する。

    Args:
        summary (RunUsageSummarySchema): サマリースキーマ。

    Returns:
        dict[str, Any]: シリアライズ済み辞書。

    Side Effects:
        なし。

    Examples:
        >>> format_output(execute_logic({"run_id": "r-1"}))["run_id"]
        'r-1'
    """
    return summary.model_dump()


def main(data: dict[str, Any]) -> dict[str, Any]:
    """入力検証・サマリー生成・辞書整形の一連の処理を実行する。

    Args:
        data (dict[str, Any]): 実行サマリー入力辞書。

    Returns:
        dict[str, Any]: 整形済みサマリー辞書。

    Side Effects:
        なし。

    Examples:
        >>> main({"run_id": "r-1", "run_type": "manual"})["run_type_label"]
        '手動更新'
    """
    validated = validate_input(data)
    summary = execute_logic(validated)
    return format_output(summary)
