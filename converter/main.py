import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .codecs import FORMATS
from .errors import APIError
from .mcp_server import asgi_app as mcp_asgi_app
from .routes import MAX_BODY_BYTES, router
from .ui import router as ui_router, static_files

log = logging.getLogger("converter")

DESCRIPTION = f"""
Convert data between **JSON, XML, YAML, CSV, Protobuf Text, FlatBuffers, Protocol Buffers, Avro, MessagePack, CBOR and BSON**.

Endpoints are named `POST /convert/{{source}}-to-{{target}}`, e.g. `/convert/json-to-xml`,
`/convert/csv-to-messagepack`. Send the source document as the raw request body.

* **Text formats** (JSON, XML, YAML, CSV, Protobuf Text): send and receive plain text.
* **Binary formats** (FlatBuffers, Protobuf, Avro, MessagePack, CBOR, BSON): send **base64 text**
  (or raw bytes with `Content-Type: application/octet-stream`); receive base64 text, or raw bytes with `?response_format=raw`.
* Max request size: {MAX_BODY_BYTES // 1024} KB.

**Errors** always look like `{{"error": {{"code", "message", "hint", ...}}}}`:
`400 INVALID_INPUT` (body isn't valid source data), `422 CONVERSION_FAILED` (valid, but not expressible in the target),
`413 PAYLOAD_TOO_LARGE`, `500 INTERNAL_ERROR`.

Schema-based formats are handled without a schema: FlatBuffers via FlexBuffers, Protobuf via `google.protobuf.Value`,
Avro via an inferred (and embedded) schema.
"""

app = FastAPI(
    title="Format Converter API",
    version="1.0.0",
    description=DESCRIPTION,
    docs_url="/docs",
    redoc_url="/redoc",
)
app.include_router(router)
app.include_router(ui_router)  # web UI at "/"
app.mount("/static", static_files, name="static")
app.add_route("/mcp", mcp_asgi_app, include_in_schema=False)  # MCP server (streamable HTTP) for AI agents


# --------------------------------------------------------------------------- error handling
@app.exception_handler(APIError)
async def api_error_handler(request: Request, exc: APIError):
    path = request.url.path
    if path.startswith("/convert/") and "-to-" in path:
        src, _, tgt = path.rsplit("/", 1)[-1].partition("-to-")
        exc.extra.setdefault("source_format", src)
        exc.extra.setdefault("target_format", tgt)
        exc.extra.setdefault("stage", "parse" if exc.code == "INVALID_INPUT" else "request")
    return JSONResponse(exc.to_dict(), status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    problems = "; ".join(
        f"{'.'.join(str(p) for p in e['loc'] if p != 'query')}: {e['msg']}" for e in exc.errors()
    )
    body = {"error": {"code": "INVALID_PARAMETER", "message": f"Invalid request parameter(s): {problems}.",
                      "stage": "request"}}
    return JSONResponse(body, status_code=422)


@app.exception_handler(StarletteHTTPException)
async def http_handler(request: Request, exc: StarletteHTTPException):
    codes = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}
    hints = {404: "See /formats for the list of available endpoints.", 405: "Conversion endpoints only accept POST."}
    message = f"{exc.detail}: {request.method} {request.url.path}" if exc.status_code == 404 else str(exc.detail)
    error = {"code": codes.get(exc.status_code, "HTTP_ERROR"), "message": message}
    if exc.status_code in hints:
        error["hint"] = hints[exc.status_code]
    return JSONResponse({"error": error}, status_code=exc.status_code, headers=getattr(exc, "headers", None))


@app.exception_handler(Exception)
async def unexpected_handler(request: Request, exc: Exception):
    log.exception("Unhandled error on %s", request.url.path)
    body = {"error": {"code": "INTERNAL_ERROR", "message": "An unexpected error occurred while converting the data.",
                      "hint": "If this keeps happening, report the input that triggered it."}}
    return JSONResponse(body, status_code=500)


# --------------------------------------------------------------------------- utility routes
@app.get("/health", tags=["Utility"], summary="Health check")
def health():
    return {"status": "ok"}


@app.get("/formats", tags=["Utility"], summary="List supported formats and conversion endpoints")
def formats():
    return {
        "formats": [
            {
                "key": f.key, "name": f.name, "binary": f.binary,
                "input": "base64 text or application/octet-stream" if f.binary else "plain text",
                "converts_to": [f"/convert/{f.key}-to-{t.key}" for t in FORMATS.values() if t is not f],
            }
            for f in FORMATS.values()
        ]
    }
