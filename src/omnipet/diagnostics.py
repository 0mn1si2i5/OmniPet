"""Bounded, non-sensitive diagnostics for generation failures."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

import openai

from omnipet.security import contains_credential_like_text


CATEGORIES = (
    "local-validation",
    "missing-credentials",
    "authentication",
    "authorization",
    "rate-limit",
    "provider-timeout",
    "provider-request",
    "provider-response",
    "deterministic-qa",
    "publication",
)
_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")


@dataclass(frozen=True)
class SafeDiagnostic:
    category: str
    status: int | None = None
    request_id: str | None = None
    retryable: bool = False

    def __post_init__(self) -> None:
        if self.category not in CATEGORIES:
            raise ValueError("diagnostic category is invalid")
        if self.status is not None and (
            isinstance(self.status, bool)
            or not isinstance(self.status, int)
            or not 100 <= self.status <= 599
        ):
            raise ValueError("diagnostic status is invalid")
        if self.request_id is not None and (
            not isinstance(self.request_id, str)
            or _REQUEST_ID.fullmatch(self.request_id) is None
            or contains_credential_like_text(self.request_id)
        ):
            raise ValueError("diagnostic request ID is invalid")
        if not isinstance(self.retryable, bool):
            raise ValueError("diagnostic retryable flag is invalid")

    def to_dict(self) -> dict[str, str | int | bool | None]:
        return {
            "category": self.category,
            "status": self.status,
            "request_id": self.request_id,
            "retryable": self.retryable,
        }

    @classmethod
    def from_dict(cls, value: Any) -> SafeDiagnostic:
        if not isinstance(value, dict) or set(value) != {
            "category", "status", "request_id", "retryable"
        }:
            raise ValueError("diagnostic schema is invalid")
        return cls(
            value["category"], value["status"], value["request_id"],
            value["retryable"],
        )


def openai_diagnostic(error: BaseException) -> SafeDiagnostic:
    if isinstance(error, openai.AuthenticationError):
        category, retryable = "authentication", False
    elif isinstance(error, openai.PermissionDeniedError):
        category, retryable = "authorization", False
    elif isinstance(error, openai.RateLimitError):
        category, retryable = "rate-limit", True
    elif isinstance(error, openai.APITimeoutError):
        category, retryable = "provider-timeout", True
    elif isinstance(error, openai.APIResponseValidationError):
        category, retryable = "provider-response", False
    elif isinstance(error, openai.APIConnectionError):
        category, retryable = "provider-request", True
    elif isinstance(error, openai.APIStatusError):
        category = "provider-request"
        retryable = error.status_code in (408, 409) or error.status_code >= 500
    else:
        return SafeDiagnostic("provider-request", retryable=False)

    status = error.status_code if isinstance(error, openai.APIStatusError) else None
    request_id = error.request_id if isinstance(error, openai.APIStatusError) else None
    if (
        not isinstance(request_id, str)
        or _REQUEST_ID.fullmatch(request_id) is None
        or contains_credential_like_text(request_id)
    ):
        request_id = None
    return SafeDiagnostic(category, status, request_id, retryable)
