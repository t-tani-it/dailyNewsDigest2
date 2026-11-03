"""ユーザー設定更新・表示用のPydanticスキーマ。

目的（Why）:
    設定画面および基本画面から送信されるカテゴリ選択、表示件数（1〜15件）、
    並び替え順、概要使用文字数（0〜500文字）、LLMモデル選択設定を検証する（指示書 §13, §17.1, §17.6）。
仕様（What）:
    `UserSettingUpdateSchema` および `UserSettingResponseSchema` の定義。
前提条件・依存関係（Assumption / Dependencies）:
    pydantic>=2.7, `app.core.config`
入出力（I/O）:
    設定APIリクエストの検証済みモデル。
注意点（Caution）:
    有料API使用を許可するフィールドは一切受け付けない（`extra="forbid"` 相当または無視＋ポリシー固定）。
今後の拡張ポイント（Future Work）:
    曜日別スケジュール設定の追加。
変更履歴（Change Log）:
    2026-11-02 - 初版作成（Step 2-1）
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.config import (
    ALLOW_PAID_LLM_EXECUTION,
    DEFAULT_DISPLAY_COUNT,
    DEFAULT_EXCLUDED_CATEGORIES,
    DEFAULT_SELECTED_CATEGORIES,
    DEFAULT_SUMMARY_CHAR_LIMIT,
    EXCLUDED_CATEGORIES,
    MAX_DISPLAY_COUNT,
    MAX_SUMMARY_CHAR_LIMIT,
    MIN_DISPLAY_COUNT,
    MIN_SUMMARY_CHAR_LIMIT,
    NEWS_CATEGORIES,
)

# =============================================================================
# 設定値ブロック
# =============================================================================

TIME_FORMAT_REGEX: str = r"^(?:[01]\d|2[0-3]):[0-5]\d$"


class UserSettingUpdateSchema(BaseModel):
    """ユーザー設定の部分更新リクエストスキーマ。"""

    selected_categories: list[str] | None = None
    excluded_categories: list[str] | None = None
    display_count: int | None = Field(
        default=None, ge=MIN_DISPLAY_COUNT, le=MAX_DISPLAY_COUNT
    )
    sort_order: Literal["importance", "latest"] | None = None
    target_days: int | None = Field(default=None, ge=1, le=7)
    auto_update_enabled: bool | None = None
    auto_update_time: str | None = Field(default=None, pattern=TIME_FORMAT_REGEX)
    summary_char_limit: int | None = Field(
        default=None, ge=MIN_SUMMARY_CHAR_LIMIT, le=MAX_SUMMARY_CHAR_LIMIT
    )
    llm_enabled: bool | None = None
    selection_mode: Literal["auto", "gemini", "openrouter"] | None = None
    preferred_model_id: str | None = None
    auto_switch_enabled: bool | None = None
    daily_api_call_limit: int | None = Field(default=None, ge=1, le=5000)

    @field_validator("selected_categories")
    @classmethod
    def check_selected_categories(cls, values: list[str] | None) -> list[str] | None:
        """選択カテゴリが定義済みカテゴリに含まれるか検証する。"""
        if values is None:
            return None
        cleaned = [c for c in dict.fromkeys(values) if c in NEWS_CATEGORIES]
        if not cleaned:
            raise ValueError("興味カテゴリを少なくとも1つ選択してください。")
        return cleaned

    @field_validator("excluded_categories")
    @classmethod
    def check_excluded_categories(cls, values: list[str] | None) -> list[str] | None:
        """除外カテゴリが定義済み除外カテゴリに含まれるか検証する。"""
        if values is None:
            return None
        return [c for c in dict.fromkeys(values) if c in EXCLUDED_CATEGORIES]


class UserSettingResponseSchema(BaseModel):
    """ユーザー設定の応答スキーマ（有料モデル実行ポリシーは常に False 固定）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int = 1
    is_initial_approved: bool = False
    selected_categories: list[str] = Field(
        default_factory=lambda: list(DEFAULT_SELECTED_CATEGORIES)
    )
    excluded_categories: list[str] = Field(
        default_factory=lambda: list(DEFAULT_EXCLUDED_CATEGORIES)
    )
    display_count: int = DEFAULT_DISPLAY_COUNT
    sort_order: str = "importance"
    target_days: int = 1
    auto_update_enabled: bool = False
    auto_update_time: str = "07:00"
    summary_char_limit: int = DEFAULT_SUMMARY_CHAR_LIMIT
    llm_enabled: bool = False
    selection_mode: str = "auto"
    preferred_model_id: str | None = None
    auto_switch_enabled: bool = True
    daily_api_call_limit: int = 200
    last_successful_run_at: datetime | None = None
    allow_paid_llm_execution: bool = ALLOW_PAID_LLM_EXECUTION


def validate_input(data: dict[str, Any]) -> dict[str, Any]:
    """設定更新入力辞書を検証する。

    Args:
        data (dict[str, Any]): 更新対象フィールドを含む辞書。

    Returns:
        dict[str, Any]: 検証済み辞書。

    Raises:
        ValueError: 有料LLM実行を有効化しようとするキーが含まれる場合。

    Side Effects:
        なし。

    Examples:
        >>> validate_input({"display_count": 10})["display_count"]
        10
    """
    if data.get("allow_paid_llm_execution") is True or data.get("allow_paid_llm") is True:
        raise ValueError("有料LLMの実行はポリシーにより設定画面から有効化できません。")
    return dict(data)


def execute_logic(validated_data: dict[str, Any]) -> UserSettingUpdateSchema:
    """検証済み辞書から `UserSettingUpdateSchema` を生成する。

    Args:
        validated_data (dict[str, Any]): `validate_input` 通過後の辞書。

    Returns:
        UserSettingUpdateSchema: 検証済み更新スキーマ。

    Side Effects:
        なし。

    Examples:
        >>> execute_logic({"display_count": 10}).display_count
        10
    """
    return UserSettingUpdateSchema(**validated_data)


def format_output(schema_obj: UserSettingUpdateSchema) -> dict[str, Any]:
    """None 以外の変更対象フィールドのみを辞書として返す。

    Args:
        schema_obj (UserSettingUpdateSchema): 更新スキーマオブジェクト。

    Returns:
        dict[str, Any]: 変更対象フィールドの辞書。

    Side Effects:
        なし。

    Examples:
        >>> format_output(execute_logic({"display_count": 8}))
        {'display_count': 8}
    """
    return schema_obj.model_dump(exclude_none=True)


def main(data: dict[str, Any]) -> dict[str, Any]:
    """入力検証・スキーマ変換・変更フィールド抽出の一連の処理を実行する。

    Args:
        data (dict[str, Any]): 設定更新入力辞書。

    Returns:
        dict[str, Any]: 更新適用用の辞書。

    Side Effects:
        なし。

    Examples:
        >>> main({"sort_order": "latest"})
        {'sort_order': 'latest'}
    """
    validated = validate_input(data)
    schema_obj = execute_logic(validated)
    return format_output(schema_obj)
