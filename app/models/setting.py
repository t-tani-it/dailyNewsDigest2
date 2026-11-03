"""ユーザー設定（user_settings）のSQLAlchemy ORMモデル。

目的（Why）:
    初回承認状態、興味カテゴリ、除外カテゴリ、表示件数（1〜15件）、並び替え順、
    概要使用文字数（0〜500文字）、LLMモデル選択モード、自動切替ON/OFF、
    および二重実行防止用ロック情報を永続化する（指示書 §6, §13, §17.6, §19, §20）。
仕様（What）:
    `UserSetting` テーブル定義とデフォルト設定生成。
前提条件・依存関係（Assumption / Dependencies）:
    sqlalchemy>=2.0, `app.core.config`, `app.core.database`, `app.models.rss_source`
入出力（I/O）:
    PostgreSQL / SQLite 上の `user_settings` テーブルレコード（シングルトン id=1）。
注意点（Caution）:
    設定画面から有料モデル実行を有効化できるカラムは一切持たせないこと（指示書 §11）。
今後の拡張ポイント（Future Work）:
    複数プロファイル切替への対応。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 1-4）
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import (
    DEFAULT_DISPLAY_COUNT,
    DEFAULT_EXCLUDED_CATEGORIES,
    DEFAULT_SELECTED_CATEGORIES,
    DEFAULT_SUMMARY_CHAR_LIMIT,
    MAX_DISPLAY_COUNT,
    MAX_SUMMARY_CHAR_LIMIT,
    MIN_DISPLAY_COUNT,
    MIN_SUMMARY_CHAR_LIMIT,
)
from app.core.database import Base
from app.models.rss_source import utc_now

# =============================================================================
# 設定値ブロック（並び替え・モデル選択モード定数）
# =============================================================================

SORT_ORDER_IMPORTANCE: str = "importance"
SORT_ORDER_LATEST: str = "latest"
VALID_SORT_ORDERS: tuple[str, ...] = (SORT_ORDER_IMPORTANCE, SORT_ORDER_LATEST)

SELECTION_MODE_AUTO: str = "auto"
SELECTION_MODE_GEMINI: str = "gemini"
SELECTION_MODE_OPENROUTER: str = "openrouter"
VALID_SELECTION_MODES: tuple[str, ...] = (
    SELECTION_MODE_AUTO,
    SELECTION_MODE_GEMINI,
    SELECTION_MODE_OPENROUTER,
)


class UserSetting(Base):
    """ユーザー設定テーブル（`user_settings`）。"""

    __tablename__ = "user_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    # 初回起動確認で「はい」を選択したかどうか（指示書 §6.3）
    is_initial_approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    selected_categories: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=lambda: list(DEFAULT_SELECTED_CATEGORIES)
    )
    excluded_categories: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=lambda: list(DEFAULT_EXCLUDED_CATEGORIES)
    )
    display_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_DISPLAY_COUNT
    )
    sort_order: Mapped[str] = mapped_column(
        String(30), nullable=False, default=SORT_ORDER_IMPORTANCE
    )
    target_days: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    auto_update_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    auto_update_time: Mapped[str] = mapped_column(String(10), nullable=False, default="07:00")
    summary_char_limit: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_SUMMARY_CHAR_LIMIT
    )
    llm_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    selection_mode: Mapped[str] = mapped_column(
        String(30), nullable=False, default=SELECTION_MODE_AUTO
    )
    preferred_model_id: Mapped[str | None] = mapped_column(String(150), nullable=True)
    auto_switch_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    daily_api_call_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=200)
    last_successful_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 定期実行と手動更新の二重実行防止ロック情報（指示書 §20）
    active_run_lock_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    active_run_locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


def validate_input(data: dict[str, Any] | None = None) -> dict[str, Any]:
    """ユーザー設定更新辞書の値範囲・妥当性を検証する。

    Args:
        data (dict[str, Any] | None): 設定値辞書。

    Returns:
        dict[str, Any]: 検証済み辞書。

    Raises:
        ValueError: 表示件数（1〜15）や概要文字数（0〜500）等が範囲外の場合。

    Side Effects:
        なし。

    Examples:
        >>> validate_input({"display_count": 10})["display_count"]
        10
    """
    if not data:
        return {}
    cleaned = dict(data)

    if "display_count" in cleaned:
        count = int(cleaned["display_count"])
        if not (MIN_DISPLAY_COUNT <= count <= MAX_DISPLAY_COUNT):
            raise ValueError(f"display_count は {MIN_DISPLAY_COUNT}〜{MAX_DISPLAY_COUNT} の範囲で指定してください。")
        cleaned["display_count"] = count

    if "summary_char_limit" in cleaned:
        char_limit = int(cleaned["summary_char_limit"])
        if not (MIN_SUMMARY_CHAR_LIMIT <= char_limit <= MAX_SUMMARY_CHAR_LIMIT):
            raise ValueError(
                f"summary_char_limit は {MIN_SUMMARY_CHAR_LIMIT}〜{MAX_SUMMARY_CHAR_LIMIT} の範囲で指定してください。"
            )
        cleaned["summary_char_limit"] = char_limit

    if "sort_order" in cleaned and cleaned["sort_order"] not in VALID_SORT_ORDERS:
        raise ValueError(f"未対応の sort_order です: {cleaned['sort_order']}")

    if "selection_mode" in cleaned and cleaned["selection_mode"] not in VALID_SELECTION_MODES:
        raise ValueError(f"未対応の selection_mode です: {cleaned['selection_mode']}")

    return cleaned


def execute_logic(validated_data: dict[str, Any]) -> UserSetting:
    """検証済み辞書から `UserSetting` インスタンスを生成する。

    Args:
        validated_data (dict[str, Any]): `validate_input` 通過後の辞書。

    Returns:
        UserSetting: ORMモデルインスタンス。

    Side Effects:
        なし。

    Examples:
        >>> s = execute_logic({"display_count": 12})
        >>> s.display_count
        12
    """
    return UserSetting(
        id=1,
        is_initial_approved=bool(validated_data.get("is_initial_approved", False)),
        selected_categories=list(
            validated_data.get("selected_categories", DEFAULT_SELECTED_CATEGORIES)
        ),
        excluded_categories=list(
            validated_data.get("excluded_categories", DEFAULT_EXCLUDED_CATEGORIES)
        ),
        display_count=int(validated_data.get("display_count", DEFAULT_DISPLAY_COUNT)),
        sort_order=str(validated_data.get("sort_order", SORT_ORDER_IMPORTANCE)),
        target_days=int(validated_data.get("target_days", 1)),
        auto_update_enabled=bool(validated_data.get("auto_update_enabled", False)),
        auto_update_time=str(validated_data.get("auto_update_time", "07:00")),
        summary_char_limit=int(
            validated_data.get("summary_char_limit", DEFAULT_SUMMARY_CHAR_LIMIT)
        ),
        llm_enabled=bool(validated_data.get("llm_enabled", False)),
        selection_mode=str(validated_data.get("selection_mode", SELECTION_MODE_AUTO)),
        preferred_model_id=validated_data.get("preferred_model_id"),
        auto_switch_enabled=bool(validated_data.get("auto_switch_enabled", True)),
        daily_api_call_limit=int(validated_data.get("daily_api_call_limit", 200)),
    )


def format_output(setting_obj: UserSetting) -> dict[str, Any]:
    """`UserSetting` インスタンスを画面・API用辞書へ整形する。

    Args:
        setting_obj (UserSetting): ユーザー設定ORMインスタンス。

    Returns:
        dict[str, Any]: 整形済み辞書。

    Side Effects:
        なし。

    Examples:
        >>> format_output(execute_logic({}))["display_count"]
        15
    """
    return {
        "id": setting_obj.id,
        "is_initial_approved": setting_obj.is_initial_approved,
        "selected_categories": setting_obj.selected_categories,
        "excluded_categories": setting_obj.excluded_categories,
        "display_count": setting_obj.display_count,
        "sort_order": setting_obj.sort_order,
        "target_days": setting_obj.target_days,
        "auto_update_enabled": setting_obj.auto_update_enabled,
        "auto_update_time": setting_obj.auto_update_time,
        "summary_char_limit": setting_obj.summary_char_limit,
        "llm_enabled": setting_obj.llm_enabled,
        "selection_mode": setting_obj.selection_mode,
        "preferred_model_id": setting_obj.preferred_model_id,
        "auto_switch_enabled": setting_obj.auto_switch_enabled,
        "daily_api_call_limit": setting_obj.daily_api_call_limit,
        "last_successful_run_at": setting_obj.last_successful_run_at,
    }


def main(data: dict[str, Any] | None = None) -> dict[str, Any]:
    """入力検証・設定モデル生成・辞書整形の一連の処理を実行する。

    Args:
        data (dict[str, Any] | None): 設定データ辞書。

    Returns:
        dict[str, Any]: 整形済み辞書。

    Side Effects:
        なし。

    Examples:
        >>> main({"display_count": 5})["display_count"]
        5
    """
    validated = validate_input(data)
    instance = execute_logic(validated)
    return format_output(instance)
