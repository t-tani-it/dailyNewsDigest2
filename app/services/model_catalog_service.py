"""無料LLMモデル自動発見・許可リスト照合・カタログ同期サービス。

目的（Why）:
    Google AI Studio（Gemini）および OpenRouter のモデル一覧を動的に取得・照合し、
    「無料利用が確認できたモデル（`verified_free`）」と「要確認（`unverified`）」
    「有料または条件付き（`paid_or_conditional`）」を厳格に区別してDB管理する
    （指示書 §10, §11, §12.1, §13, §16）。
仕様（What）:
    - `seed_default_models_and_prices`: 初期無料モデル候補と比較対象有料3モデルの初期レコード登録
    - `upsert_discovered_models`: 発見されたモデル群の無料条件検証と `model_catalog` への保存
    - `sync_price_snapshots_from_openrouter_entries`: 比較用有料モデルの公式単価スナップショット更新
    - `refresh_model_catalog`: Gemini / OpenRouter 両プロバイダーからの一覧取得とカタログ更新
    - `update_model_availability`: レート制限・無料枠上限・認証エラー発生時のモデル状態更新
    - `list_catalog_models`: 画面表示およびルーター選定用のモデル一覧取得
前提条件・依存関係（Assumption / Dependencies）:
    sqlalchemy>=2.0, `app.models.model_catalog`, `app.providers`, `app.schemas.llm_schema`
入出力（I/O）:
    入力: SQLAlchemy `Session`、各Providerインスタンス
    出力: `ModelCatalog` / `PriceSnapshot` リストおよび同期結果サマリー辞書
注意点（Caution）:
    - 比較用の有料3モデルは `price_snapshots` に単価のみ記録し、実行可能な無料モデルとして登録してはならない。
    - 価格が確認できない有料モデルは `is_verified=False`（画面表示「算出不可」）のまま維持する。
今後の拡張ポイント（Future Work）:
    モデル一覧の定期バックグラウンド自動同期ジョブの追加。
変更履歴（Change Log）:
    2026-11-03 - 初版作成（Step 4-2）
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import (
    COMPARISON_PAID_MODELS,
    GEMINI_FREE_TIER_ALLOWLIST,
)
from app.core.logging import get_logger
from app.models.model_catalog import (
    AVAILABILITY_AVAILABLE,
    FREE_STATUS_PAID_OR_CONDITIONAL,
    FREE_STATUS_VERIFIED,
    ModelCatalog,
    PriceSnapshot,
)
from app.models.rss_source import utc_now
from app.providers.gemini_provider import GeminiProvider
from app.providers.openrouter_provider import (
    OpenRouterProvider,
    parse_openrouter_per_token_to_per_1m,
)
from app.schemas.llm_schema import PAID_COMPARISON_MODEL_IDS, DiscoveredModelSchema

# =============================================================================
# 設定値ブロック（初期シード定義・公式ソースURL）
# =============================================================================

OPENROUTER_MODELS_SOURCE_URL: str = "https://openrouter.ai/api/v1/models"

DEFAULT_FREE_MODEL_SEEDS: tuple[dict[str, Any], ...] = (
    {
        "model_id": "openrouter/free",
        "provider": "openrouter",
        "model_name": "OpenRouter Free Router (無料自動ルーティング)",
        "free_tier_status": FREE_STATUS_VERIFIED,
        "input_price_per_1m": 0.0,
        "output_price_per_1m": 0.0,
        "request_price": 0.0,
        "supports_structured_output": False,
        "availability_status": AVAILABILITY_AVAILABLE,
        "source_info": "OpenRouter Free Router default",
    },
    {
        "model_id": GEMINI_FREE_TIER_ALLOWLIST[0],
        "provider": "gemini",
        "model_name": "Gemini 2.0 Flash (Free Tier 許可リスト確認済み)",
        "free_tier_status": FREE_STATUS_VERIFIED,
        "input_price_per_1m": 0.0,
        "output_price_per_1m": 0.0,
        "request_price": 0.0,
        "supports_structured_output": True,
        "availability_status": AVAILABILITY_AVAILABLE,
        "source_info": "Google AI Studio Free Tier allowlist",
    },
    {
        "model_id": GEMINI_FREE_TIER_ALLOWLIST[1],
        "provider": "gemini",
        "model_name": "Gemini 2.0 Flash-Lite (Free Tier 許可リスト確認済み)",
        "free_tier_status": FREE_STATUS_VERIFIED,
        "input_price_per_1m": 0.0,
        "output_price_per_1m": 0.0,
        "request_price": 0.0,
        "supports_structured_output": True,
        "availability_status": AVAILABILITY_AVAILABLE,
        "source_info": "Google AI Studio Free Tier allowlist",
    },
)

logger = get_logger(__name__)


def seed_default_models_and_prices(session: Session) -> list[ModelCatalog]:
    """モデルカタログと有料比較モデル価格スナップショットの初期レコードを登録する。

    解説：
        1. `model_catalog` に初期の無料確認済みモデル候補が無ければ登録する。
        2. `price_snapshots` に比較対象有料3モデル（`deepseek/deepseek-v4.1-flash`,
           `openai/gpt-6-luna`, `gemini-3.8-flash`）が無ければ、
           価格未確認（`is_verified=False`, 単価 `None`＝「算出不可」）として枠を作成しておく。

    Args:
        session (Session): SQLAlchemy セッション。

    Returns:
        list[ModelCatalog]: カタログ内の全モデルリスト。

    Side Effects:
        不足している初期レコードを INSERT し commit する。
    """
    changed = False

    for seed in DEFAULT_FREE_MODEL_SEEDS:
        existing_model = session.get(ModelCatalog, str(seed["model_id"]))
        if existing_model is None:
            session.add(
                ModelCatalog(
                    model_id=str(seed["model_id"]),
                    provider=str(seed["provider"]),
                    model_name=str(seed["model_name"]),
                    free_tier_status=str(seed["free_tier_status"]),
                    input_price_per_1m=float(seed["input_price_per_1m"]),
                    output_price_per_1m=float(seed["output_price_per_1m"]),
                    request_price=float(seed["request_price"]),
                    supports_structured_output=bool(seed["supports_structured_output"]),
                    availability_status=str(seed["availability_status"]),
                    source_info=str(seed["source_info"]),
                )
            )
            changed = True

    for paid_def in COMPARISON_PAID_MODELS:
        mid = str(paid_def["model_id"])
        stmt = select(PriceSnapshot).where(PriceSnapshot.model_id == mid)
        existing_snap = session.scalar(stmt)
        if existing_snap is None:
            in_p = paid_def.get("default_input_price_per_1m")
            out_p = paid_def.get("default_output_price_per_1m")
            is_ver = in_p is not None and out_p is not None
            session.add(
                PriceSnapshot(
                    model_id=mid,
                    model_name=str(paid_def["model_name"]),
                    provider=str(paid_def["provider"]),
                    input_price_per_1m=float(in_p) if in_p is not None else None,
                    output_price_per_1m=float(out_p) if out_p is not None else None,
                    price_tier="standard",
                    is_verified=is_ver,
                    verified_at=utc_now() if is_ver else None,
                    source_url=None,
                )
            )
            changed = True

    if changed:
        session.commit()

    return list(
        session.scalars(
            select(ModelCatalog).order_by(
                ModelCatalog.provider.asc(),
                ModelCatalog.model_id.asc(),
            )
        ).all()
    )


def upsert_discovered_models(
    session: Session,
    discovered: list[DiscoveredModelSchema],
) -> list[ModelCatalog]:
    """APIから発見されたモデル情報を `model_catalog` へ登録・更新する（指示書 §12.1）。

    解説：
        - 有料比較対象モデル（`PAID_COMPARISON_MODEL_IDS`）や正の単価を持つモデルは、
          万が一入力で `verified_free` となっていても強制的に `paid_or_conditional` に降格する。
        - 既存モデルの `availability_status` は最新確認時点で `available` に更新する。

    Args:
        session (Session): SQLAlchemy セッション。
        discovered (list[DiscoveredModelSchema]): Providerから取得したモデル情報リスト。

    Returns:
        list[ModelCatalog]: 登録・更新されたモデルレコード一覧。

    Side Effects:
        `model_catalog` テーブルへの INSERT / UPDATE と commit を行う。
    """
    now = utc_now()
    upserted: list[ModelCatalog] = []

    for item in discovered:
        has_positive_price = any(
            p is not None and float(p) > 0.0
            for p in (item.input_price_per_1m, item.output_price_per_1m, item.request_price)
        )
        if item.model_id in PAID_COMPARISON_MODEL_IDS or has_positive_price:
            safe_free_status = FREE_STATUS_PAID_OR_CONDITIONAL
        else:
            safe_free_status = item.free_tier_status

        existing = session.get(ModelCatalog, item.model_id)
        if existing is None:
            existing = ModelCatalog(
                model_id=item.model_id,
                provider=item.provider,
                model_name=item.model_name,
            )
            session.add(existing)

        existing.provider = item.provider
        existing.model_name = item.model_name
        existing.free_tier_status = safe_free_status
        existing.input_price_per_1m = item.input_price_per_1m
        existing.output_price_per_1m = item.output_price_per_1m
        existing.request_price = item.request_price
        existing.context_length = item.context_length
        existing.modalities = list(item.modalities or ["text"])
        existing.supports_structured_output = bool(item.supports_structured_output)
        existing.availability_status = item.availability_status
        existing.checked_at = now
        existing.source_info = item.source_info
        upserted.append(existing)

    session.commit()
    return upserted


def sync_price_snapshots_from_openrouter_entries(
    session: Session,
    raw_entries: list[dict[str, Any]],
) -> list[PriceSnapshot]:
    """OpenRouter `/models` の生データ内に比較対象有料モデルがあれば単価スナップショットを更新する（指示書 §16.2）。

    解説：
        比較対象3モデル（`deepseek/deepseek-v4.1-flash`, `openai/gpt-6-luna` 等）の単価が
        OpenRouter 公式 `/models` API レスポンスに含まれている場合のみ、
        確認日時（`verified_at`）と取得元URL付きで `price_snapshots` を更新する。
        API結果に含まれないモデルは推測せず未確認（「算出不可」）のまま維持する。

    Args:
        session (Session): SQLAlchemy セッション。
        raw_entries (list[dict[str, Any]]): OpenRouter `/models` の `data` 配列。

    Returns:
        list[PriceSnapshot]: 更新された `PriceSnapshot` リスト。

    Side Effects:
        該当モデルが存在する場合に `price_snapshots` を UPDATE し commit する。
    """
    seed_default_models_and_prices(session)
    by_id = {
        str(entry.get("id") or "").strip(): entry
        for entry in raw_entries
        if isinstance(entry, dict) and entry.get("id")
    }

    updated_snapshots: list[PriceSnapshot] = []
    now = utc_now()

    for paid_def in COMPARISON_PAID_MODELS:
        mid = str(paid_def["model_id"])
        raw = by_id.get(mid)
        if not raw:
            continue

        pricing = raw.get("pricing") if isinstance(raw.get("pricing"), dict) else {}
        in_price = parse_openrouter_per_token_to_per_1m(pricing.get("prompt"))
        out_price = parse_openrouter_per_token_to_per_1m(pricing.get("completion"))
        if in_price is None or out_price is None:
            continue

        stmt = select(PriceSnapshot).where(PriceSnapshot.model_id == mid)
        snap = session.scalar(stmt)
        if snap is None:
            snap = PriceSnapshot(
                model_id=mid,
                model_name=str(paid_def["model_name"]),
                provider=str(paid_def["provider"]),
            )
            session.add(snap)

        snap.input_price_per_1m = in_price
        snap.output_price_per_1m = out_price
        snap.is_verified = True
        snap.verified_at = now
        snap.source_url = OPENROUTER_MODELS_SOURCE_URL
        updated_snapshots.append(snap)

    if updated_snapshots:
        session.commit()

    return updated_snapshots


def update_model_availability(
    session: Session,
    model_id: str,
    availability_status: str,
) -> ModelCatalog | None:
    """指定モデルの利用可能状態（`available`, `rate_limited`, `quota_exhausted` 等）を更新する（指示書 §12.2）。

    Args:
        session (Session): SQLAlchemy セッション。
        model_id (str): 更新対象モデルID。
        availability_status (str): 新しい利用可能ステータス。

    Returns:
        ModelCatalog | None: 更新後のモデル（未登録時は None）。

    Side Effects:
        `model_catalog.availability_status` と `checked_at` を UPDATE し commit する。
    """
    model_obj = session.get(ModelCatalog, model_id)
    if model_obj is None:
        return None
    model_obj.availability_status = str(availability_status)
    model_obj.checked_at = utc_now()
    session.commit()
    session.refresh(model_obj)
    return model_obj


def list_catalog_models(
    session: Session,
    executable_only: bool = False,
    provider: str | None = None,
) -> list[ModelCatalog]:
    """モデルカタログ一覧を取得する。

    Args:
        session (Session): SQLAlchemy セッション。
        executable_only (bool): True の場合、`is_executable_free_model == True` のみ返す。
        provider (str | None): `"gemini"` または `"openrouter"` で絞り込む場合に指定。

    Returns:
        list[ModelCatalog]: 条件に一致するモデルリスト。

    Side Effects:
        カタログが空の場合に初期シードを登録する。
    """
    all_models = seed_default_models_and_prices(session)
    filtered = all_models
    if provider:
        filtered = [m for m in filtered if m.provider == provider]
    if executable_only:
        filtered = [m for m in filtered if m.is_executable_free_model]
    return filtered


def refresh_model_catalog(
    session: Session,
    gemini_provider: GeminiProvider | None = None,
    openrouter_provider: OpenRouterProvider | None = None,
) -> dict[str, Any]:
    """Gemini と OpenRouter から最新モデル一覧を取得し、カタログと価格スナップショットを更新する（指示書 §10.3, §12.1）。

    Args:
        session (Session): SQLAlchemy セッション。
        gemini_provider (GeminiProvider | None): Gemini アダプタ（省略時は標準設定で生成）。
        openrouter_provider (OpenRouterProvider | None): OpenRouter アダプタ（省略時は標準設定で生成）。

    Returns:
        dict[str, Any]: 更新件数・実行可能無料モデル数・モデル一覧を含む辞書。

    Side Effects:
        外部APIからモデル一覧を取得し、`model_catalog` と `price_snapshots` を更新する。
    """
    seed_default_models_and_prices(session)
    gem = gemini_provider or GeminiProvider()
    orc = openrouter_provider or OpenRouterProvider()

    discovered: list[DiscoveredModelSchema] = []

    gem_models = gem.list_available_models()
    discovered.extend(gem_models)

    raw_or_entries = orc.fetch_raw_models_list()
    if raw_or_entries:
        sync_price_snapshots_from_openrouter_entries(session, raw_or_entries)
        from app.providers.openrouter_provider import parse_openrouter_model_entry

        for entry in raw_or_entries:
            parsed = parse_openrouter_model_entry(entry)
            if parsed is not None and parsed.free_tier_status == FREE_STATUS_VERIFIED:
                discovered.append(parsed)

    if discovered:
        upsert_discovered_models(session, discovered)

    all_models = list_catalog_models(session, executable_only=False)
    executable_models = [m for m in all_models if m.is_executable_free_model]

    return {
        "discovered_count": len(discovered),
        "total_models_count": len(all_models),
        "executable_free_count": len(executable_models),
        "models": all_models,
    }


def validate_input(data: dict[str, Any]) -> dict[str, Any]:
    """カタログサービスの入力辞書を検証する。

    Args:
        data (dict[str, Any]): `session` を含む辞書。

    Returns:
        dict[str, Any]: 検証済み辞書。

    Raises:
        ValueError: `session` が無効な場合。

    Side Effects:
        なし。
    """
    if not isinstance(data, dict) or not isinstance(data.get("session"), Session):
        raise ValueError("有効な SQLAlchemy Session が必要です。")
    return dict(data)


def execute_logic(validated: dict[str, Any]) -> dict[str, Any]:
    """指定されたアクション（`list` または `refresh`）を実行する。"""
    session: Session = validated["session"]
    action = str(validated.get("action", "list"))
    if action == "refresh":
        return refresh_model_catalog(
            session=session,
            gemini_provider=validated.get("gemini_provider"),
            openrouter_provider=validated.get("openrouter_provider"),
        )
    models = list_catalog_models(
        session=session,
        executable_only=bool(validated.get("executable_only", False)),
        provider=validated.get("provider"),
    )
    return {
        "total_models_count": len(models),
        "executable_free_count": sum(1 for m in models if m.is_executable_free_model),
        "models": models,
    }


def format_output(result: dict[str, Any]) -> dict[str, Any]:
    """モデルリストを画面・API返却用の辞書へ整形する。"""
    models: list[ModelCatalog] = result.get("models") or []
    return {
        "total_models_count": result.get("total_models_count", len(models)),
        "executable_free_count": result.get("executable_free_count", 0),
        "models": [
            {
                "model_id": m.model_id,
                "provider": m.provider,
                "model_name": m.model_name,
                "free_tier_status": m.free_tier_status,
                "availability_status": m.availability_status,
                "supports_structured_output": m.supports_structured_output,
                "is_executable_free_model": m.is_executable_free_model,
            }
            for m in models
        ],
    }


def main(data: dict[str, Any]) -> dict[str, Any]:
    """モデルカタログの取得・更新・整形を一括実行する。"""
    validated = validate_input(data)
    res = execute_logic(validated)
    return format_output(res)
