"""Exception types and the JSON error envelope returned by every failing request."""
from typing import Any, Optional

from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    code: str = Field(..., examples=["INVALID_INPUT"], description="Stable, machine-readable error code.")
    message: str = Field(..., description="Human-readable explanation of what went wrong.")
    hint: Optional[str] = Field(None, description="Suggestion for fixing the request.")
    source_format: Optional[str] = Field(None, examples=["json"])
    target_format: Optional[str] = Field(None, examples=["xml"])
    stage: Optional[str] = Field(None, examples=["parse"], description="`request`, `parse` (reading the input) or `serialize` (writing the output).")


class ErrorResponse(BaseModel):
    error: ErrorDetail


class APIError(Exception):
    status_code = 400
    code = "BAD_REQUEST"

    def __init__(self, message: str, *, hint: Optional[str] = None, **extra: Any):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.extra = extra

    def to_dict(self) -> dict:
        body = {"code": self.code, "message": self.message, "hint": self.hint, **self.extra}
        return {"error": {k: v for k, v in body.items() if v is not None}}


class InvalidInputError(APIError):
    """The request body is not valid data in the declared source format (HTTP 400)."""
    status_code = 400
    code = "INVALID_INPUT"


class InvalidFormatError(InvalidInputError):
    """A format name that isn't supported (HTTP 400 / MCP tool error)."""
    code = "INVALID_FORMAT"


class InvalidSchemaError(InvalidInputError):
    """A schema option (e.g. proto_schema) that can't be compiled or doesn't match (HTTP 400)."""
    code = "INVALID_SCHEMA"


class ConversionError(APIError):
    """The input was valid, but cannot be represented in the target format (HTTP 422)."""
    status_code = 422
    code = "CONVERSION_FAILED"


class OutputTooLargeError(APIError):
    """The converted result is too big to return to an agent (MCP only)."""
    status_code = 413
    code = "OUTPUT_TOO_LARGE"


class PayloadTooLargeError(APIError):
    status_code = 413
    code = "PAYLOAD_TOO_LARGE"
