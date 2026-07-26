from __future__ import annotations

import re


MAX_SCANNED_TEXT_LENGTH = 1_000_000
_CREDENTIAL_TEXT = re.compile(
    r"(?i)(?:\bauthorization\s*[:=]\s*\S+|\bbearer\s+[a-z0-9._-]+|"
    r"\b(?:[a-z][a-z0-9]*_)?api[_ -]?key`?\s*[:=]\s*\S+|"
    r"\b(?:access[_ -]?token|refresh[_ -]?token|token|password|"
    r"session[_ -]?cookie)\s*[:=]\s*\S+|\bsk-[a-z0-9_-]{8,})"
)
_ABSOLUTE_PATH_TEXT = re.compile(
    r"(?i)(?:"
    r"(?:^|[\s\"'`(=])/(?!/)[^\s\"'`)]*"
    r"|\bfile:///[^\s\"'`)]*"
    r"|(?:^|[\s\"'`(=])\\\\[^\\/\s]+\\[^\s\"'`)]*"
    r"|(?:^|[\s\"'`(=])[A-Z]:\\[^\r\n\"'`)]*"
    r")"
)
_PROHIBITED_RELEASE_TEXT = re.compile(
    r"(?i)(?:"
    r"(?:^|[/\\\s`\"'])pet\.yaml(?:$|[/\\\s`\"'.,;:])"
    r"|(?:^|[/\\\s`\"'])brief\.md(?:$|[/\\\s`\"'.,;:])"
    r"|(?:^|[/\\\s`\"'])(?:BUDGET\.md|PROVENANCE\.json)(?:$|[/\\\s`\"'.,;:])"
    r"|(?:^|[/\\\s`\"'])\.omnipet(?:$|[/\\\s`\"'.,;:])"
    r"|(?:^|[\s`\"'])(?:references|prompts|qa)[/\\]"
    r"|\b(?:answer[- ]?key|provider metadata|reviewer source verdict)s?\b"
    r"|\bcheckpoint(?:s)?\b"
    r")"
)


def is_credential_like_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
    clear_markers = (
        "apikey",
        "accesstoken",
        "refreshtoken",
        "clientsecret",
        "authorization",
        "privatekey",
        "signingkey",
        "sessioncookie",
        "authcookie",
        "password",
        "credential",
        "secret",
    )
    return (
        normalized in {"auth", "bearer", "cookie", "token"}
        or any(marker in normalized for marker in clear_markers)
        or normalized.endswith(("token", "secret"))
    )


def contains_credential_like_text(value: str) -> bool:
    return len(value) > MAX_SCANNED_TEXT_LENGTH or _CREDENTIAL_TEXT.search(value) is not None


def contains_absolute_path_text(value: str) -> bool:
    return _ABSOLUTE_PATH_TEXT.search(value) is not None


def contains_prohibited_release_text(value: str) -> bool:
    return _PROHIBITED_RELEASE_TEXT.search(value) is not None
