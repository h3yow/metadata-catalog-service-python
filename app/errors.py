"""错误类型与错误码。"""


class CatalogError(Exception):
    """目录服务错误基类。"""

    code = "CATALOG_ERROR"
    status_code = 400

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(CatalogError):
    code = "NOT_FOUND"
    status_code = 404


class ForbiddenError(CatalogError):
    code = "FORBIDDEN"
    status_code = 403


class ConflictError(CatalogError):
    code = "CONFLICT"
    status_code = 409


class ValidationError(CatalogError):
    code = "VALIDATION_ERROR"
    status_code = 422


class CyclicLineageError(CatalogError):
    code = "CYCLE_DETECTED"
    status_code = 409


class IllegalRenameError(ValidationError):
    """字段名对不上又未在 renames 中显式声明：疑似非法改名。"""

    code = "ILLEGAL_RENAME"


class ImplicitDropError(ValidationError):
    """旧字段在新版本消失且未在 drops 中显式声明。"""

    code = "IMPLICIT_DROP"


class TypeIncompatibleError(ValidationError):
    code = "TYPE_INCOMPATIBLE"


class MissingDefaultError(ValidationError):
    code = "MISSING_DEFAULT"


class RenameConflictError(ValidationError):
    code = "RENAME_CONFLICT"


class VersionConflictError(ConflictError):
    code = "VERSION_CONFLICT"


class DuplicateEntryError(ConflictError):
    code = "DUPLICATE_ENTRY"
