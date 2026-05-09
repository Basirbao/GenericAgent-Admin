"""Recoverable chat-stream retry policy and error classification.

The retry decision lives on the backend because the agent stream can finish with
transport errors embedded in the final assistant text. Keep marker matching in
this module so adding future recoverable errors is localized.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .. import _paths

CONFIG_KEY = "chat_error_retry"
DEFAULT_ENABLED = True
DEFAULT_MAX_ATTEMPTS = 2
MAX_CONFIG_ATTEMPTS = 5
_FINAL_MARKER_WINDOW_CHARS = 500


@dataclass(frozen=True)
class ChatRetryConfig:
    enabled: bool = DEFAULT_ENABLED
    max_attempts: int = DEFAULT_MAX_ATTEMPTS

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "max_attempts": int(self.max_attempts),
        }


@dataclass(frozen=True)
class RecoverableErrorMatch:
    code: str
    label: str
    marker: str

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "label": self.label,
            "marker": self.marker,
        }


@dataclass(frozen=True)
class RecoverableErrorPattern:
    code: str
    label: str
    pattern: re.Pattern[str]


_RECOVERABLE_ERROR_PATTERNS = (
    RecoverableErrorPattern(
        code="ssl_error",
        label="SSLError",
        pattern=re.compile(
            r"!!!\s*Error:\s*(?:requests\.)?(?:exceptions\.)?SSLError\b[^\r\n]*\s*\Z",
            re.IGNORECASE,
        ),
    ),
)


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    if value is None:
        return default
    return bool(value)


def normalize_chat_retry_config(payload: Mapping[str, Any] | None) -> ChatRetryConfig:
    raw = payload if isinstance(payload, Mapping) else {}
    enabled = _coerce_bool(raw.get("enabled"), DEFAULT_ENABLED)
    try:
        max_attempts = int(raw.get("max_attempts", DEFAULT_MAX_ATTEMPTS))
    except (TypeError, ValueError):
        max_attempts = DEFAULT_MAX_ATTEMPTS
    max_attempts = max(0, min(MAX_CONFIG_ATTEMPTS, max_attempts))
    return ChatRetryConfig(enabled=enabled, max_attempts=max_attempts)


def load_chat_retry_config() -> ChatRetryConfig:
    cfg = _paths.load_config()
    payload = cfg.get(CONFIG_KEY)
    return normalize_chat_retry_config(payload if isinstance(payload, Mapping) else None)


def save_chat_retry_config(payload: Mapping[str, Any] | None) -> ChatRetryConfig:
    cfg = _paths.load_config()
    normalized = normalize_chat_retry_config(payload)
    cfg[CONFIG_KEY] = normalized.to_dict()
    _paths.save_config(cfg)
    return normalized


def classify_recoverable_error(content: str) -> RecoverableErrorMatch | None:
    """Return the recoverable stream error near the final output, if any."""
    final_text = (content or "").rstrip()[-_FINAL_MARKER_WINDOW_CHARS:]
    if not final_text:
        return None
    for spec in _RECOVERABLE_ERROR_PATTERNS:
        match = spec.pattern.search(final_text)
        if match:
            return RecoverableErrorMatch(
                code=spec.code,
                label=spec.label,
                marker=match.group(0),
            )
    return None
