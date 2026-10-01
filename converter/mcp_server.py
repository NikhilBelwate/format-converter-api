"""MCP server exposing the format converter to AI agents.

Run over stdio:        python -m converter.mcp_server
Streamable HTTP:       mounted at /mcp by converter.main

Every failure is returned as a tool result with `isError: true` whose text is the same JSON
error envelope the REST API uses, so the calling model can read the `hint` and retry.
"""
import base64
import functools
import json
import logging
import os
import sys
from typing import Annotated, Any, Dict, Literal, Optional

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from .codecs import FORMAT_NOTES, FORMATS
from .errors import APIError, InvalidInputError, OutputTooLargeError, PayloadTooLargeError
from .service import convert, decode_base64, decode_payload, get_format

log = logging.getLogger("converter.mcp")

# Module-level so they can be tuned via environment variables (and patched in tests).
MAX_INPUT_BYTES = int(os.environ.get("MAX_BODY_BYTES", 4 * 1024 * 1024))
MAX_OUTPUT_BYTES = int(os.environ.get("MCP_MAX_OUTPUT_BYTES", 1024 * 1024))

FORMAT_KEYS = list(FORMATS)
FormatArg = Annotated[
    str,
    Field(
        description="Format key: " + ", ".join(FORMAT_KEYS) + ". Common aliases (e.g. 'msgpack') are accepted.",
        json_schema_extra={"enum": FORMAT_KEYS},
    ),
]

INSTRUCTIONS = (
    "Converts data between JSON, XML, YAML, CSV, FlatBuffers, Protocol Buffers, Avro, MessagePack, CBOR and BSON. "
    "Text formats (json, xml, yaml, csv) are passed and returned as plain strings. "
    "Binary formats (flatbuffers, protobuf, avro, messagepack, cbor, bson) are passed and returned as BASE64 strings. "
    "Call list_formats for per-format caveats. Errors come back as JSON with a 'code', 'message' and 'hint' - "
    "read the hint and retry with corrected input."
)

mcp = MCPServer("format-converter", instructions=INSTRUCTIONS)

_READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)


# --------------------------------------------------------------------------- error handling
def get_format_key(value: Any) -> str:
    try:
        return get_format(value).key
    except APIError:
        return str(value)


def _envelope(exc: APIError, source: Optional[str] = None, target: Optional[str] = None) -> str:
    if exc.code != "INVALID_FORMAT":  # the formats are unknown/bad there; echoing them adds nothing
        if source:
            exc.extra.setdefault("source_format", get_format_key(source))
        if target:
            exc.extra.setdefault("target_format", get_format_key(target))
    if exc.code in ("INVALID_INPUT", "INVALID_FORMAT"):
        exc.extra.setdefault("stage", "parse" if exc.code == "INVALID_INPUT" else "request")
    return json.dumps(exc.to_dict(), ensure_ascii=False)


def tool_errors(fn):
    """Turn every failure into an `isError` tool result carrying the standard error envelope."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except APIError as exc:
            raise ToolError(_envelope(exc, kwargs.get("source_format") or kwargs.get("format"), kwargs.get("target_format")))
        except RecursionError:
            raise ToolError(_envelope(InvalidInputError("Data is nested too deeply to process.")))
        except Exception:  # never leak internals or crash the server
            log.exception("Unexpected error in tool %s", fn.__name__)
            body = {"error": {"code": "INTERNAL_ERROR",
                              "message": "An unexpected error occurred while converting the data.",
                              "hint": "Retry with simpler input; if it keeps failing, report the input that triggered it."}}
            raise ToolError(json.dumps(body))
    return wrapper


def _check_input_size(data: str) -> None:
    size = len(data.encode("utf-8"))
    if size > MAX_INPUT_BYTES:
        raise PayloadTooLargeError(
            f"Input is {size} bytes; the limit is {MAX_INPUT_BYTES}.",
            hint="Convert a smaller document, or split the data into chunks.",
        )


def _require_text(data: Any) -> str:
    if not isinstance(data, str):
        raise InvalidInputError(
            f"'data' must be a string, got {type(data).__name__}.",
            hint="Pass the document as a string (JSON/XML/YAML/CSV text, or base64 text for binary formats).",
        )
    return data


# --------------------------------------------------------------------------- tools
class ConversionOutput(BaseModel):
    output: str = Field(description="The converted document: plain text, or base64 text when encoding is 'base64'.")
    encoding: Literal["text", "base64"] = Field(description="'base64' for binary target formats, otherwise 'text'.")
    media_type: str
    size_bytes: int = Field(description="Size of the converted document in bytes (before base64).")
    source_format: str
    target_format: str


@mcp.tool(annotations=_READ_ONLY)
@tool_errors
def convert_data(
    source_format: FormatArg,
    target_format: FormatArg,
    data: Annotated[str, Field(description="The document to convert. Text for json/xml/yaml/csv; base64 text for binary formats.")],
    pretty: Annotated[bool, Field(description="Indent JSON/XML/YAML output.")] = True,
    xml_root: Annotated[str, Field(description="Root element name when writing XML from data without a single root (e.g. arrays).")] = "root",
    infer_types: Annotated[bool, Field(description="CSV input only: turn values like 30, 9.5, true into numbers/booleans.")] = False,
) -> ConversionOutput:
    """Convert a document from one format to another.

    Example: source_format="json", target_format="xml", data='{"user": {"name": "Ann"}}'
    returns XML text. Example: source_format="json", target_format="messagepack" returns
    base64 text (encoding="base64"); feed that string back with source_format="messagepack".
    """
    data = _require_text(data)
    src, tgt = get_format(source_format), get_format(target_format)
    _check_input_size(data)
    result = convert(src.key, tgt.key, data, {"pretty": pretty, "xml_root": xml_root, "infer_types": infer_types})
    if len(result.payload) > MAX_OUTPUT_BYTES:
        raise OutputTooLargeError(
            f"The converted {tgt.name} is {len(result.payload)} bytes; the limit for tool results is {MAX_OUTPUT_BYTES}.",
            hint="Convert a smaller document, or use the REST API for large payloads.",
        )
    output = base64.b64encode(result.payload).decode("ascii") if tgt.binary else result.payload.decode("utf-8")
    return ConversionOutput(
        output=output, encoding="base64" if tgt.binary else "text", media_type=tgt.media_type,
        size_bytes=len(result.payload), source_format=src.key, target_format=tgt.key,
    )


class ValidationOutput(BaseModel):
    valid: bool
    format: str
    summary: Optional[str] = Field(None, description="When valid: the top-level structure that was parsed.")
    error: Optional[Dict[str, Any]] = Field(None, description="When invalid: the error envelope contents (code, message, hint).")


def _summarize(data: Any) -> str:
    if isinstance(data, dict):
        return f"object with {len(data)} key(s)"
    if isinstance(data, list):
        return f"array with {len(data)} item(s)"
    return f"{type(data).__name__} value"


@mcp.tool(annotations=_READ_ONLY)
@tool_errors
def validate_data(
    format: FormatArg,
    data: Annotated[str, Field(description="The document to check. Text for json/xml/yaml/csv; base64 text for binary formats.")],
) -> ValidationOutput:
    """Check whether `data` is well-formed in the given format, without converting it.

    Returns valid=false with the parse error (instead of a tool error) when the document is malformed,
    so it is a cheap way to test input before calling convert_data.
    """
    data = _require_text(data)
    fmt = get_format(format)
    _check_input_size(data)
    try:
        if not data.strip():
            raise InvalidInputError("Input data is empty.", hint=f"Provide the {fmt.name} document to check.")
        raw = decode_base64(data, fmt) if fmt.binary else data.encode("utf-8")
        parsed = decode_payload(fmt, raw, {})
    except InvalidInputError as exc:
        return ValidationOutput(valid=False, format=fmt.key, error=json.loads(_envelope(exc, fmt.key))["error"])
    return ValidationOutput(valid=True, format=fmt.key, summary=_summarize(parsed))


class FormatInfo(BaseModel):
    key: str
    name: str
    binary: bool
    data_encoding: str = Field(description="How `data` is passed/returned: 'plain text' or 'base64 string'.")
    media_type: str
    notes: Optional[str] = None


class FormatsOutput(BaseModel):
    formats: list[FormatInfo]
    options: Dict[str, str]
    usage: str


@mcp.tool(annotations=_READ_ONLY)
@tool_errors
def list_formats() -> FormatsOutput:
    """List the supported formats, how data is passed for each, and per-format caveats. Call this first if unsure."""
    return FormatsOutput(
        formats=[
            FormatInfo(key=f.key, name=f.name, binary=f.binary,
                       data_encoding="base64 string" if f.binary else "plain text",
                       media_type=f.media_type, notes=FORMAT_NOTES.get(f.key))
            for f in FORMATS.values()
        ],
        options={"pretty": "JSON/XML/YAML output", "xml_root": "XML output", "infer_types": "CSV input"},
        usage="Call convert_data(source_format, target_format, data). Any format can be converted to any other.",
    )


# --------------------------------------------------------------------------- HTTP transport
class MCPHttpApp:
    """Streamable-HTTP endpoint for FastAPI/Starlette (stateless, one JSON response per request).

    A short-lived session manager is created per request so this works without ASGI lifespan events
    (not guaranteed on serverless platforms). The server holds no session state, so nothing is lost.
    A class instance (not a function) so Starlette's Route treats it as a raw ASGI app.
    """

    async def __call__(self, scope, receive, send) -> None:
        manager = StreamableHTTPSessionManager(app=mcp._lowlevel_server, json_response=True, stateless=True)
        async with manager.run():
            await manager.handle_request(scope, receive, send)


asgi_app = MCPHttpApp()


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)  # stdout is reserved for the protocol
    mcp.run("stdio")


if __name__ == "__main__":
    main()
