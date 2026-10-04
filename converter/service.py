"""Transport-independent conversion pipeline shared by the REST routes and the MCP server."""
import base64
import binascii
import difflib
import inspect
from dataclasses import dataclass
from typing import Any, Dict, Union

from .codecs import FORMATS, Format
from .errors import APIError, ConversionError, InvalidFormatError, InvalidInputError
from .normalize import to_plain


def get_format(key: Any) -> Format:
    """Look up a format by key, tolerating case/whitespace, with a helpful error for typos."""
    normalized = str(key).strip().lower().replace(" ", "").replace("-", "").replace("_", "")
    aliases = {"msgpack": "messagepack", "protocolbuffers": "protobuf", "proto": "protobuf",
               "flexbuffers": "flatbuffers", "yml": "yaml",
               "textproto": "prototext", "txtpb": "prototext", "pbtxt": "prototext", "prototxt": "prototext",
               "protobuftext": "prototext", "prototextformat": "prototext"}
    normalized = aliases.get(normalized, normalized)
    if normalized in FORMATS:
        return FORMATS[normalized]
    close = difflib.get_close_matches(normalized, list(FORMATS), n=1)
    suggestion = f" Did you mean '{close[0]}'?" if close else ""
    raise InvalidFormatError(
        f"Unknown format '{key}'.{suggestion}",
        hint=f"Supported formats: {', '.join(FORMATS)}.",
    )


def decode_base64(body: Union[bytes, str], fmt: Format) -> bytes:
    try:
        text = body.decode("ascii") if isinstance(body, bytes) else body
        return base64.b64decode("".join(text.split()), validate=True)
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise InvalidInputError(
            f"{fmt.name} is a binary format, so the data must be base64 text: {exc}.",
            hint="Base64-encode the raw bytes before sending them.",
        )


def decode_payload(src: Format, raw: bytes, options: Dict[str, Any]) -> Any:
    accepted = inspect.signature(src.decode).parameters
    try:
        return to_plain(src.decode(raw, **{k: v for k, v in options.items() if k in accepted}))
    except APIError:
        raise
    except RecursionError:
        raise InvalidInputError("Data is nested too deeply to process.")
    except Exception as exc:  # decoder library surprised us
        raise InvalidInputError(f"Could not read input as {src.name}: {exc or type(exc).__name__}.")


def encode_payload(tgt: Format, data: Any, options: Dict[str, Any]) -> bytes:
    accepted = inspect.signature(tgt.encode).parameters
    try:
        return tgt.encode(data, **{k: v for k, v in options.items() if k in accepted})
    except APIError as exc:
        exc.extra.setdefault("stage", "serialize")
        raise
    except RecursionError:
        raise ConversionError("Data is nested too deeply to process.", stage="serialize")
    except Exception as exc:
        raise ConversionError(f"Data cannot be written as {tgt.name}: {exc or type(exc).__name__}.", stage="serialize")


@dataclass(frozen=True)
class ConversionResult:
    payload: bytes
    source: Format
    target: Format


def convert(source_key: str, target_key: str, data: Union[bytes, str], options: Dict[str, Any]) -> ConversionResult:
    """Convert `data` (text, or base64 text for binary sources) between two formats."""
    src, tgt = get_format(source_key), get_format(target_key)
    raw = data.encode("utf-8") if isinstance(data, str) else data
    if not raw.strip():
        raise InvalidInputError("Input data is empty.", hint=f"Provide the {src.name} document to convert.")
    if src.binary:
        raw = decode_base64(raw, src)
    return ConversionResult(encode_payload(tgt, decode_payload(src, raw, options), options), src, tgt)
