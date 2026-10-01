"""One endpoint per (source, target) pair: POST /convert/{source}-to-{target}."""
import base64
import inspect
import os
from typing import Any, Dict

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from .codecs import FORMAT_NOTES, FORMATS, Format
from .errors import ErrorResponse, InvalidInputError, PayloadTooLargeError
from .normalize import to_plain
from .service import decode_base64, decode_payload, encode_payload

MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", 4 * 1024 * 1024))  # Vercel's hard limit is 4.5 MB

router = APIRouter(prefix="/convert")


# --------------------------------------------------------------------------- pipeline
async def read_body(request: Request, src: Format) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise PayloadTooLargeError(f"Request body is larger than {MAX_BODY_BYTES} bytes.")
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise PayloadTooLargeError(f"Request body is larger than {MAX_BODY_BYTES} bytes.")
    if not body.strip():
        raise InvalidInputError(
            "Request body is empty.",
            hint=f"Send the {src.name} document as the raw request body.",
        )
    if src.binary and not request.headers.get("content-type", "").startswith("application/octet-stream"):
        return decode_base64(body, src)
    return body


def build_response(tgt: Format, payload: bytes, options: Dict[str, Any]) -> Response:
    if not tgt.binary:
        return Response(payload, media_type=f"{tgt.media_type}; charset=utf-8")
    if options.get("response_format") == "raw":
        return Response(
            payload, media_type=tgt.media_type,
            headers={"Content-Disposition": f'attachment; filename="converted.{tgt.extension}"'},
        )
    return Response(base64.b64encode(payload), media_type="text/plain; charset=utf-8")


# --------------------------------------------------------------------------- endpoint factory
def _options_for(src: Format, tgt: Format) -> Dict[str, tuple]:
    """Query parameters that make sense for this particular pair: name -> (type, default, description)."""
    opts: Dict[str, tuple] = {}
    if src.key == "csv":
        opts["infer_types"] = (bool, False, "Turn CSV values like `30`, `9.5`, `true` into numbers/booleans instead of strings.")
    if tgt.key in ("json", "xml", "yaml"):
        opts["pretty"] = (bool, True, "Indent the output for readability.")
    if tgt.key == "xml":
        opts["xml_root"] = (str, "root", "Root element name, used when the data has no single root (e.g. arrays).")
    if tgt.binary:
        opts["response_format"] = (str, "base64", "`base64` returns the bytes as base64 text; `raw` returns a binary download.")
    return opts


def _describe(src: Format, tgt: Format) -> str:
    lines = [f"Converts a **{src.name}** document into **{tgt.name}**."]
    if src.binary:
        lines.append(f"\n**Input:** the {src.name} bytes, base64-encoded, as the raw request body "
                     "(or send raw bytes with `Content-Type: application/octet-stream`).")
    else:
        lines.append(f"\n**Input:** the {src.name} text as the raw request body.")
    if tgt.binary:
        lines.append(f"\n**Output:** {tgt.name} bytes as base64 text (use `response_format=raw` for a binary download).")
    else:
        lines.append(f"\n**Output:** {tgt.name} text (`{tgt.media_type}`).")
    for fmt in (src, tgt):
        if fmt.key in FORMAT_NOTES:
            lines.append(f"\n*{fmt.name}:* {FORMAT_NOTES[fmt.key]}")
    return "\n".join(lines)


def _make_endpoint(src: Format, tgt: Format):
    opts = _options_for(src, tgt)

    async def endpoint(request: Request, **params: Any) -> Response:
        raw = await read_body(request, src)
        payload = encode_payload(tgt, decode_payload(src, raw, params), params)
        return build_response(tgt, payload, params)

    # Give FastAPI a real signature so each endpoint shows only its relevant query parameters in Swagger.
    parameters = [inspect.Parameter("request", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Request)]
    for name, (typ, default, desc) in opts.items():
        parameters.append(inspect.Parameter(
            name, inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=typ,
            default=Query(default, description=desc,
                          **({"pattern": "^(base64|raw)$"} if name == "response_format" else {})),
        ))
    endpoint.__signature__ = inspect.Signature(parameters)
    endpoint.__name__ = f"{src.key}_to_{tgt.key}"
    return endpoint


def _example_body(src: Format) -> str:
    if not src.binary:
        return src.sample
    json_sample = FORMATS["json"].sample
    data = to_plain(FORMATS["json"].decode(json_sample.encode()))
    return base64.b64encode(src.encode(data)).decode("ascii")


def _register() -> None:
    examples = {key: _example_body(f) for key, f in FORMATS.items()}
    for src in FORMATS.values():
        for tgt in FORMATS.values():
            if src is tgt:
                continue
            content = {"text/plain": {"schema": {"type": "string"}, "example": examples[src.key]}}
            if src.binary:
                content["application/octet-stream"] = {"schema": {"type": "string", "format": "binary"}}
            ok_content = {f"{tgt.media_type}": {"schema": {"type": "string", **({"format": "binary"} if tgt.binary else {})}}}
            if tgt.binary:
                ok_content["text/plain"] = {"schema": {"type": "string", "format": "byte"}}
            router.add_api_route(
                f"/{src.key}-to-{tgt.key}",
                _make_endpoint(src, tgt),
                methods=["POST"],
                name=f"{src.key}_to_{tgt.key}",
                operation_id=f"{src.key}_to_{tgt.key}",
                summary=f"Convert {src.name} to {tgt.name}",
                description=_describe(src, tgt),
                tags=[f"From {src.name}"],
                response_class=Response,
                responses={
                    200: {"description": f"{tgt.name} output.", "content": ok_content},
                    400: {"model": ErrorResponse, "description": f"Empty body, or the body is not valid {src.name}."},
                    413: {"model": ErrorResponse, "description": "Request body too large."},
                    422: {"model": ErrorResponse, "description": f"Valid {src.name}, but it cannot be represented as {tgt.name}; or an invalid query parameter."},
                    500: {"model": ErrorResponse, "description": "Unexpected server error."},
                },
                openapi_extra={"requestBody": {"required": True, "content": content}},
            )


_register()
