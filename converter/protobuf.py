"""Protocol Buffers codec.

Protobuf bytes carry only field numbers and wire types, never field names, so there are two modes:

* **Schema mode** (`proto_schema` given): the .proto text is compiled with protoc (from grpcio-tools)
  and the bytes are read/written as that message, with real field names and types.
* **Raw mode** (no schema): like `protoc --decode_raw`, fields are keyed by number (`{"1": "Ann", "2": 42}`).
  Types are inferred from the wire format, so this is a best-effort view; encoding needs numeric keys.

The Protobuf text format (`name: "Ann" id: 42`, the readable form of a message object) always needs the schema.
It goes through the binary encoding, so it gets the same checks as Protobuf bytes.
"""
import base64
import functools
import inspect
import math
import os
import re
import struct
import subprocess
import sys
import tempfile
from typing import Any, Dict, Optional, Tuple

from google.protobuf import descriptor_pb2, descriptor_pool, json_format, message_factory, text_format
from google.protobuf.message import DecodeError

from .errors import ConversionError, InvalidInputError, InvalidSchemaError

MAX_SCHEMA_BYTES = 256 * 1024
PROTOC_TIMEOUT_SECONDS = 20
MAX_FIELD_NUMBER = 2**29 - 1
MAX_RAW_DEPTH = 100
_ABSL_LOG = re.compile(r"^(WARNING: All log messages|[IWEF]\d{4} )")  # protoc log lines that aren't errors

# Matches the JSON and Protobuf samples in codecs.FORMATS; used as the Swagger example for proto_schema.
EXAMPLE_SCHEMA = ('syntax = "proto3"; message Person { message Address { string city = 1; } string name = 1; '
                  'int32 age = 2; double score = 3; bool active = 4; repeated string tags = 5; Address address = 6; }')

SCHEMA_HINT = "Pass the .proto definition as proto_schema (and proto_message if it defines several messages)."

# Newer protobuf releases can emit int64 fields as JSON numbers instead of strings; use it when available.
_TO_DICT_OPTIONS: Dict[str, Any] = {"preserving_proto_field_name": True}
if "unquote_int64_if_possible" in inspect.signature(json_format.MessageToDict).parameters:
    _TO_DICT_OPTIONS["unquote_int64_if_possible"] = True


# --------------------------------------------------------------------------- codec entry points
def dec_protobuf(raw: bytes, proto_schema: Optional[str] = None, proto_message: Optional[str] = None) -> Any:
    if proto_schema:
        return _decode_with_schema(raw, _message_class(proto_schema, proto_message))
    if proto_message:
        raise _message_without_schema()
    try:
        return _parse_raw(raw, 0)
    except _NotAMessage as exc:
        raise InvalidInputError(
            f"Invalid Protobuf data: {exc}.",
            hint="Check that the bytes are a serialized Protobuf message (base64-encoded). " + SCHEMA_HINT,
        )


def enc_protobuf(data: Any, proto_schema: Optional[str] = None, proto_message: Optional[str] = None) -> bytes:
    if proto_schema:
        return _encode_with_schema(data, _message_class(proto_schema, proto_message))
    if proto_message:
        raise _message_without_schema()
    if not isinstance(data, dict):
        raise ConversionError(
            f"A Protobuf message must be an object, got {_kind(data)}.",
            hint=SCHEMA_HINT,
        )
    return _encode_raw(data, "")


def dec_prototext(raw: bytes, proto_schema: Optional[str] = None, proto_message: Optional[str] = None) -> Any:
    cls = _message_class(_require_schema(proto_schema), proto_message)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InvalidInputError(f"Input is not valid UTF-8 text (invalid byte at position {exc.start}).")
    msg = cls()
    try:
        text_format.Parse(text, msg)
    except text_format.ParseError as exc:
        raise InvalidInputError(
            f"Invalid Protobuf text for message {cls.DESCRIPTOR.full_name}: {str(exc).rstrip('.')}.",
            hint='Use the Protobuf text format, e.g. name: "Ann" id: 42 address { city: "Paris" }.',
        )
    # Object -> Protobuf bytes -> data: exactly what a program does when it serializes the message.
    return _decode_with_schema(msg.SerializeToString(), cls)


def enc_prototext(data: Any, proto_schema: Optional[str] = None, proto_message: Optional[str] = None,
                  pretty: bool = True) -> bytes:
    cls = _message_class(_require_schema(proto_schema), proto_message)
    msg = cls.FromString(_encode_with_schema(data, cls))
    return text_format.MessageToString(msg, as_one_line=not pretty).strip().encode("utf-8") + b"\n"


def _require_schema(proto_schema: Optional[str]) -> str:
    if not proto_schema:
        raise InvalidSchemaError(
            "The Protobuf text format needs proto_schema: field names and types come from the .proto definition.",
            hint=SCHEMA_HINT, stage="request",
        )
    return proto_schema


def _message_without_schema() -> InvalidSchemaError:
    return InvalidSchemaError("proto_message was given without proto_schema.", hint=SCHEMA_HINT, stage="request")


# --------------------------------------------------------------------------- schema mode
@functools.lru_cache(maxsize=32)
def _compile(schema: str) -> descriptor_pool.DescriptorPool:
    """Compile .proto text (saved as schema.proto) into a descriptor pool."""
    if len(schema.encode("utf-8")) > MAX_SCHEMA_BYTES:
        raise InvalidSchemaError(f"proto_schema is larger than {MAX_SCHEMA_BYTES} bytes.", stage="request")
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "schema.proto"), "w", encoding="utf-8") as fh:
            fh.write(schema)
        out = os.path.join(tmp, "schema.desc")
        # A subprocess (not grpc_tools.protoc.main in-process) so protoc's error output can be captured.
        cmd = [sys.executable, "-m", "grpc_tools.protoc", f"-I{tmp}", f"--descriptor_set_out={out}",
               "--include_imports", "schema.proto"]
        try:
            proc = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True, timeout=PROTOC_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            raise InvalidSchemaError("Compiling proto_schema timed out.", stage="request")
        if proc.returncode != 0:
            details = _protoc_errors(proc.stderr, tmp)
            raise InvalidSchemaError(
                f"proto_schema does not compile: {details or 'protoc failed'}.",
                hint="Send the full .proto file text (syntax line, package, messages). "
                     "Imports other than google/protobuf/*.proto are not available.",
                stage="request",
            )
        with open(out, "rb") as fh:
            files = descriptor_pb2.FileDescriptorSet.FromString(fh.read()).file
    pool = descriptor_pool.DescriptorPool()
    for file_proto in files:  # --include_imports lists dependencies first
        pool.Add(file_proto)
    return pool


def _protoc_errors(stderr: str, tmp: str) -> str:
    """protoc's error lines ("schema.proto:3:5: ..."), without abseil log noise or the temp directory."""
    lines = [line.replace(tmp + os.sep, "").strip() for line in stderr.splitlines()]
    errors = [line for line in lines if line.startswith("schema.proto:")]
    return "; ".join(errors or [line for line in lines if line and not _ABSL_LOG.match(line)]).rstrip(".")


def _message_class(schema: str, name: Optional[str]):
    pool = _compile(schema)
    file_desc = pool.FindFileByName("schema.proto")
    top_level = list(file_desc.message_types_by_name.values())
    available = ", ".join(m.full_name for m in top_level) or "none"
    if not name:
        if len(top_level) != 1:
            raise InvalidSchemaError(
                f"proto_schema defines {len(top_level)} top-level messages; choose one with proto_message.",
                hint=f"Available messages: {available}.", stage="request",
            )
        return message_factory.GetMessageClass(top_level[0])
    name = name.strip().lstrip(".")
    candidates = [name] + ([f"{file_desc.package}.{name}"] if file_desc.package else [])
    for candidate in candidates:
        try:
            return message_factory.GetMessageClass(pool.FindMessageTypeByName(candidate))
        except KeyError:
            continue
    raise InvalidSchemaError(
        f"Message '{name}' is not defined in proto_schema.",
        hint=f"Available messages: {available} (nested messages as Outer.Inner).", stage="request",
    )


def _decode_with_schema(raw: bytes, cls) -> Any:
    name = cls.DESCRIPTOR.full_name
    msg = cls()
    try:
        msg.ParseFromString(raw)
    except DecodeError as exc:
        raise InvalidInputError(f"Invalid Protobuf data for message {name}: {exc}.")
    # Fields the schema doesn't know (or whose wire type doesn't match it) land in "unknown fields",
    # which the JSON mapping silently drops. Refuse instead of losing data.
    known = cls()
    known.CopyFrom(msg)
    known.DiscardUnknownFields()
    if known.ByteSize() != msg.ByteSize():
        raise InvalidInputError(
            f"The data contains fields that message {name} does not define (or with mismatched types).",
            hint="Check that proto_schema/proto_message match the data, or omit proto_schema to see the raw fields.",
        )
    return json_format.MessageToDict(msg, **_TO_DICT_OPTIONS)


def _encode_with_schema(data: Any, cls) -> bytes:
    name = cls.DESCRIPTOR.full_name
    if not isinstance(data, dict):
        raise ConversionError(f"Message {name} must be written from an object, got {_kind(data)}.")
    msg = cls()
    try:
        json_format.ParseDict(data, msg)
    except json_format.ParseError as exc:
        raise ConversionError(
            f"Data does not match message {name}: {exc}.",
            hint="Field names must match the .proto (snake_case or lowerCamelCase) and values must fit the field types.",
        )
    return msg.SerializeToString()


# --------------------------------------------------------------------------- raw mode: decoding
class _NotAMessage(ValueError):
    pass


def _read_varint(buf: bytes, pos: int) -> Tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(buf):
            raise _NotAMessage(f"truncated varint at byte {pos}")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            break
        shift += 7
        if shift >= 70:
            raise _NotAMessage(f"varint longer than 10 bytes at byte {pos}")
    if result >= 2**64:
        raise _NotAMessage(f"varint overflows 64 bits at byte {pos}")
    return result, pos


def _parse_raw(buf: bytes, depth: int) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    pos = 0
    while pos < len(buf):
        start = pos
        key, pos = _read_varint(buf, pos)
        number, wire_type = key >> 3, key & 7
        if not 1 <= number <= MAX_FIELD_NUMBER:
            raise _NotAMessage(f"invalid field number {number} at byte {start}")
        if wire_type == 0:
            value, pos = _read_varint(buf, pos)
            value = _signed64(value)
        elif wire_type == 1:
            pos = _need(buf, pos, 8)
            value = _fixed(buf[pos - 8:pos], "<d", "<Q", 1e-300, 1e300)
        elif wire_type == 5:
            pos = _need(buf, pos, 4)
            value = _fixed(buf[pos - 4:pos], "<f", "<I", 1e-30, 1e30)
        elif wire_type == 2:
            length, pos = _read_varint(buf, pos)
            pos = _need(buf, pos, length)
            value = _length_delimited(buf[pos - length:pos], depth)
        else:
            reason = "groups (wire types 3/4) are not supported" if wire_type in (3, 4) else f"invalid wire type {wire_type}"
            raise _NotAMessage(f"{reason} at byte {start}")
        _add(fields, str(number), value)
    return fields


def _need(buf: bytes, pos: int, size: int) -> int:
    if pos + size > len(buf):
        raise _NotAMessage(f"field at byte {pos} needs {size} bytes but only {len(buf) - pos} remain")
    return pos + size


def _signed64(value: int) -> int:
    # Negative int32/int64 values are sent as 10-byte two's complement varints.
    return value - 2**64 if value >= 2**63 else value


def _fixed(chunk: bytes, float_fmt: str, int_fmt: str, tiny: float, huge: float) -> Any:
    # double/float fields are far more common than fixed64/fixed32; fall back to the integer when the
    # bits don't look like a sensible floating-point number (e.g. small integers become denormals).
    number = struct.unpack(float_fmt, chunk)[0]
    if number == 0 or (math.isfinite(number) and tiny < abs(number) < huge):
        return float(f"{number:.7g}") if float_fmt == "<f" else number
    return struct.unpack(int_fmt, chunk)[0]


def _length_delimited(chunk: bytes, depth: int) -> Any:
    """A string, a nested message, or raw bytes (base64) - the wire format doesn't say which."""
    try:
        text: Optional[str] = chunk.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if text is not None and _looks_like_text(text):
        return text
    if chunk and depth < MAX_RAW_DEPTH:
        try:
            return _parse_raw(chunk, depth + 1)
        except _NotAMessage:
            pass
    if text is not None:
        return text
    return base64.b64encode(chunk).decode("ascii")


def _looks_like_text(text: str) -> bool:
    if not text:
        return True
    if not text[0].isprintable():  # nested messages usually start with a control byte (tags 0x08, 0x0a, 0x12, ...)
        return False
    return all(ch.isprintable() or ch in "\t\r\n" for ch in text)


def _add(fields: Dict[str, Any], key: str, value: Any) -> None:
    # Decoded values are never lists themselves, so a list always means a repeated field.
    if key not in fields:
        fields[key] = value
    elif isinstance(fields[key], list):
        fields[key].append(value)
    else:
        fields[key] = [fields[key], value]


# --------------------------------------------------------------------------- raw mode: encoding
def _encode_raw(message: Dict[str, Any], path: str) -> bytes:
    out = bytearray()
    for key, value in message.items():
        number = _field_number(key, path)
        where = f"{path}.{key}" if path else key
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, list):
                raise ConversionError(f"Field {where} contains a nested list, which Protobuf cannot represent.")
            out += _encode_field(number, item, where)
    return bytes(out)


def _field_number(key: str, path: str) -> int:
    where = f"{path}.{key}" if path else key
    if not key.isdigit() or not 1 <= int(key) <= MAX_FIELD_NUMBER:
        raise ConversionError(
            f"Without a schema, Protobuf fields must be keyed by field number (1 to {MAX_FIELD_NUMBER}); "
            f"got key '{where}'.",
            hint="Protobuf bytes do not contain field names. " + SCHEMA_HINT +
                 " Alternatively use numeric keys, e.g. {\"1\": \"Ann\", \"2\": 42}.",
        )
    return int(key)


def _encode_field(number: int, value: Any, where: str) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bool):
        return _varint(number << 3) + _varint(int(value))
    if isinstance(value, int):
        if not -(2**63) <= value < 2**64:
            raise ConversionError(f"Integer {value} in field {where} does not fit in 64 bits.")
        return _varint(number << 3) + _varint(value + 2**64 if value < 0 else value)
    if isinstance(value, float):
        return _varint(number << 3 | 1) + struct.pack("<d", value)
    if isinstance(value, str):
        payload = value.encode("utf-8")
    elif isinstance(value, dict):
        payload = _encode_raw(value, where)
    else:  # to_plain() guarantees only JSON-like values reach the encoders
        raise ConversionError(f"Field {where} has an unsupported value type {_kind(value)}.")
    return _varint(number << 3 | 2) + _varint(len(payload)) + payload


def _varint(value: int) -> bytes:
    out = bytearray()
    while value > 0x7F:
        out.append(value & 0x7F | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _kind(value: Any) -> str:
    return {dict: "an object", list: "an array", str: "a string", bool: "a boolean",
            type(None): "null"}.get(type(value), "a number")
