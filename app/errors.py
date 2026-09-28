"""Stable error codes and the HTTP-facing exception type."""
from __future__ import annotations


class ErrorCode:
    # 404 — resource does not exist (distinct from 403)
    TABLE_NOT_FOUND = "TABLE_NOT_FOUND"
    FIELD_NOT_FOUND = "FIELD_NOT_FOUND"
    VERSION_NOT_FOUND = "VERSION_NOT_FOUND"
    GLOSSARY_TERM_NOT_FOUND = "GLOSSARY_TERM_NOT_FOUND"

    # 409 — evolution / concurrency conflicts
    REVISION_MISMATCH = "REVISION_MISMATCH"
    MISSING_REQUIRED_INFO = "MISSING_REQUIRED_INFO"
    ILLEGAL_RENAME = "ILLEGAL_RENAME"
    INCOMPATIBLE_TYPE = "INCOMPATIBLE_TYPE"
    MISSING_DEFAULT_SEMANTIC = "MISSING_DEFAULT_SEMANTIC"
    ILLEGAL_DROP = "ILLEGAL_DROP"
    EMPTY_CHANGESET = "EMPTY_CHANGESET"
    CYCLE_DETECTED = "CYCLE_DETECTED"
    ENDPOINT_NOT_FOUND = "ENDPOINT_NOT_FOUND"
    FIELD_VERSION_MISMATCH = "FIELD_VERSION_MISMATCH"

    # 403 — exists but not allowed (distinct from 404)
    FORBIDDEN = "FORBIDDEN"

    # 401
    UNAUTHENTICATED = "UNAUTHENTICATED"

    # 400
    INVALID_REQUEST = "INVALID_REQUEST"


class CatalogError(Exception):
    """Business error carrying a stable code, an HTTP status and human detail."""

    def __init__(self, code: str, status_code: int, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.message = message
        self.details = details or {}
