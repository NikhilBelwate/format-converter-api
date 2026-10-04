"""Decoders/encoders for every supported format and the format registry.

Each format has `decode(bytes, **opts) -> data` and `encode(data, **opts) -> bytes`.
Decoders raise InvalidInputError for malformed input; encoders raise
ConversionError when valid data cannot be expressed in the format.
"""
import csv
import io
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional
from xml.parsers.expat import ExpatError

import cbor2
import fastavro
import msgpack
import xmltodict
import yaml
from flatbuffers import flexbuffers
import bson

from .errors import ConversionError, InvalidInputError
from .protobuf import dec_protobuf, dec_prototext, enc_protobuf, enc_prototext

INT64_MIN, INT64_MAX = -(2**63), 2**63 - 1


# --------------------------------------------------------------------------- helpers
def _text(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InvalidInputError(
            f"Input is not valid UTF-8 text (invalid byte at position {exc.start}).",
            hint="Text formats must be UTF-8 encoded. If the source is a binary format, use that format as the source endpoint.",
        )


def _walk_ints(data: Any, check: Callable[[int], None]) -> None:
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, bool):
            continue
        if isinstance(node, int):
            check(node)
        elif isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def _wrap_root(data: Any, key: str) -> Any:
    return data if isinstance(data, dict) else {key: data}


# --------------------------------------------------------------------------- JSON
def dec_json(raw: bytes) -> Any:
    try:
        return json.loads(_text(raw))
    except json.JSONDecodeError as exc:
        raise InvalidInputError(f"Invalid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno}).")


def enc_json(data: Any, pretty: bool = True) -> bytes:
    try:
        return json.dumps(data, indent=2 if pretty else None, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except ValueError as exc:
        raise ConversionError(
            "Data contains NaN or Infinity, which JSON cannot represent.",
            hint="Remove or replace those values (e.g. with null) in the source data.",
        )


# --------------------------------------------------------------------------- XML
_XML_NAME = re.compile(r"^[A-Za-z_][\w.\-]*(:[A-Za-z_][\w.\-]*)?$")


def dec_xml(raw: bytes) -> Any:
    try:
        return xmltodict.parse(_text(raw))
    except ExpatError as exc:
        raise InvalidInputError(f"Invalid XML: {exc}.", hint="Check for unclosed tags, unescaped '&' or '<', or multiple root elements.")


def _check_xml_names(node: Any, path: str = "") -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            name = key[1:] if key.startswith("@") else key
            if key != "#text" and not _XML_NAME.match(name):
                raise ConversionError(
                    f"'{key}' (at '{path or '/'}') is not a valid XML element/attribute name.",
                    hint="XML names must start with a letter or underscore and contain only letters, digits, '_', '-' or '.'.",
                )
            _check_xml_names(value, f"{path}/{key}")
    elif isinstance(node, list):
        for item in node:
            _check_xml_names(item, path)


def enc_xml(data: Any, pretty: bool = True, xml_root: str = "root") -> bytes:
    if not _XML_NAME.match(xml_root):
        raise ConversionError(f"xml_root '{xml_root}' is not a valid XML element name.")
    only_key = next(iter(data)) if isinstance(data, dict) and len(data) == 1 else None
    if only_key and only_key[0] not in "@#" and not isinstance(data[only_key], list):
        doc = data  # already has a single root element
    elif isinstance(data, list):
        doc = {xml_root: {"item": data}}
    else:
        doc = {xml_root: data}
    _check_xml_names(doc)
    try:
        return xmltodict.unparse(doc, pretty=pretty, indent="  ").encode("utf-8")
    except (ValueError, TypeError, AttributeError) as exc:
        raise ConversionError(
            f"Data cannot be written as XML: {exc}",
            hint="Arrays directly inside arrays have no XML representation; wrap them in objects.",
        )


# --------------------------------------------------------------------------- YAML
def dec_yaml(raw: bytes) -> Any:
    try:
        return yaml.safe_load(_text(raw))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
        reason = getattr(exc, "problem", None) or str(exc)
        raise InvalidInputError(f"Invalid YAML: {reason}{where}.", hint="Check indentation and quoting. Only a single YAML document is supported.")


def enc_yaml(data: Any, pretty: bool = True) -> bytes:
    return yaml.safe_dump(
        data, sort_keys=False, allow_unicode=True, default_flow_style=not pretty
    ).encode("utf-8")


# --------------------------------------------------------------------------- CSV
def _infer(value: str) -> Any:
    v = value.strip()
    low = v.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if re.fullmatch(r"[-+]?\d+", v):
        return int(v)
    if re.fullmatch(r"[-+]?(\d+\.\d*|\.\d+|\d+)([eE][-+]?\d+)?", v):
        return float(v)
    return value


def dec_csv(raw: bytes, infer_types: bool = False) -> Any:
    try:
        rows = [r for r in csv.reader(io.StringIO(_text(raw), newline="")) if r]
    except csv.Error as exc:
        raise InvalidInputError(f"Invalid CSV: {exc}.")
    if not rows:
        raise InvalidInputError("CSV input has no header row.")
    header = [h.strip() for h in rows[0]]
    if any(h == "" for h in header):
        raise InvalidInputError("CSV header contains an empty column name.", hint="Every column needs a name in the first row.")
    dupes = sorted({h for h in header if header.count(h) > 1})
    if dupes:
        raise InvalidInputError(f"CSV header has duplicate column names: {', '.join(dupes)}.")
    out: List[dict] = []
    for n, row in enumerate(rows[1:], start=2):
        if len(row) != len(header):
            raise InvalidInputError(
                f"CSV row {n} has {len(row)} field(s) but the header has {len(header)}.",
                hint="Quote fields that contain commas or line breaks.",
            )
        out.append({h: (_infer(v) if infer_types else v) for h, v in zip(header, row)})
    return out


def _csv_rows(data: Any) -> List[Any]:
    """Pick the list of records out of `data`, looking through single-key wrappers (e.g. XML roots)."""
    node = data
    for _ in range(4):
        if isinstance(node, list):
            return node
        if isinstance(node, dict) and len(node) == 1 and isinstance(next(iter(node.values())), (dict, list)):
            node = next(iter(node.values()))
        else:
            break
    # Look-ahead failed to find a list: the whole object is a single row.
    return [data]


def _flatten(node: Any, prefix: str, out: dict) -> None:
    if isinstance(node, dict) and node:
        for k, v in node.items():
            _flatten(v, f"{prefix}.{k}" if prefix else k, out)
    else:
        out[prefix] = node


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    return str(value)


def enc_csv(data: Any) -> bytes:
    rows = _csv_rows(data)
    if not rows:
        raise ConversionError("Cannot produce CSV from an empty list.", hint="Provide at least one record.")
    flat_rows = []
    for i, row in enumerate(rows):
        if isinstance(row, dict):
            flat: dict = {}
            _flatten(row, "", flat)
        elif isinstance(row, list):
            raise ConversionError(
                f"CSV needs a list of objects, but item {i} is an array.",
                hint='Use data like [{"a": 1}, {"a": 2}]. Nested objects are flattened to "parent.child" columns.',
            )
        else:
            flat = {"value": row}
        flat_rows.append(flat)
    columns: List[str] = []
    for flat in flat_rows:
        for k in flat:
            if k not in columns:
                columns.append(k)
    if not columns or columns == [""]:
        raise ConversionError("Cannot produce CSV: the records have no fields.")
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(columns)
    for flat in flat_rows:
        writer.writerow([_cell(flat.get(c)) for c in columns])
    return buf.getvalue().encode("utf-8")


# --------------------------------------------------------------------------- MessagePack
def dec_msgpack(raw: bytes) -> Any:
    try:
        return msgpack.unpackb(raw, raw=False, strict_map_key=False)
    except Exception as exc:
        raise InvalidInputError(f"Invalid MessagePack data: {exc or type(exc).__name__}.")


def enc_msgpack(data: Any) -> bytes:
    try:
        return msgpack.packb(data, use_bin_type=True)
    except (OverflowError, ValueError, TypeError) as exc:
        raise ConversionError(f"Data cannot be written as MessagePack: {exc}.")


# --------------------------------------------------------------------------- CBOR
def dec_cbor(raw: bytes) -> Any:
    try:
        return cbor2.loads(raw)
    except Exception as exc:
        raise InvalidInputError(f"Invalid CBOR data: {exc or type(exc).__name__}.")


def enc_cbor(data: Any) -> bytes:
    try:
        return cbor2.dumps(data)
    except Exception as exc:
        raise ConversionError(f"Data cannot be written as CBOR: {exc}.")


# --------------------------------------------------------------------------- BSON
def dec_bson(raw: bytes) -> Any:
    try:
        return bson.decode(raw)
    except Exception as exc:
        raise InvalidInputError(f"Invalid BSON data: {exc or type(exc).__name__}.")


def enc_bson(data: Any) -> bytes:
    def check(n: int) -> None:
        if not INT64_MIN <= n <= INT64_MAX:
            raise ConversionError(f"Integer {n} does not fit BSON's 64-bit integer range.", hint="Send very large integers as strings.")

    _walk_ints(data, check)
    try:
        return bson.encode(_wrap_root(data, "data"))
    except Exception as exc:
        raise ConversionError(f"Data cannot be written as BSON: {exc}.")


# --------------------------------------------------------------------------- Avro
_AVRO_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class _AvroInferrer:
    """Infers an Avro schema from sample data (records are merged, differing types become unions)."""

    def __init__(self) -> None:
        self.counter = 0

    def infer(self, v: Any) -> Any:
        if v is None:
            return "null"
        if isinstance(v, bool):
            return "boolean"
        if isinstance(v, int):
            if not INT64_MIN <= v <= INT64_MAX:
                raise ConversionError(f"Integer {v} does not fit Avro's 64-bit 'long' type.", hint="Send very large integers as strings.")
            return "long"
        if isinstance(v, float):
            return "double"
        if isinstance(v, str):
            return "string"
        if isinstance(v, list):
            return {"type": "array", "items": self.merge([self.infer(i) for i in v] or ["null"])}
        if isinstance(v, dict):
            self.counter += 1
            name = f"Record{self.counter}"  # captured before children bump the counter
            fields = []
            for k, val in v.items():
                if not _AVRO_NAME.match(k):
                    raise ConversionError(
                        f"Key '{k}' is not a valid Avro field name.",
                        hint="Avro field names must match [A-Za-z_][A-Za-z0-9_]*. Rename the key (e.g. use underscores instead of '-').",
                    )
                fields.append(self._field(k, self.infer(val)))
            return {"type": "record", "name": name, "fields": fields}
        raise ConversionError(f"Unsupported value type for Avro: {type(v).__name__}.")

    @staticmethod
    def _field(name: str, typ: Any) -> dict:
        field = {"name": name, "type": typ}
        if typ == "null" or (isinstance(typ, list) and typ[0] == "null"):
            field["default"] = None
        return field

    def merge(self, schemas: List[Any]) -> Any:
        flat: List[Any] = []
        for s in schemas:
            flat.extend(s if isinstance(s, list) else [s])
        simple: List[Any] = []
        record: Optional[dict] = None
        array: Optional[dict] = None
        for s in flat:
            if isinstance(s, dict) and s["type"] == "record":
                record = self._merge_records(record, s) if record else s
            elif isinstance(s, dict) and s["type"] == "array":
                array = {"type": "array", "items": self.merge([array["items"], s["items"]])} if array else s
            elif s not in simple:
                simple.append(s)
        result = simple + [x for x in (record, array) if x]
        result.sort(key=lambda s: s != "null")  # null first so it can be the default
        return result[0] if len(result) == 1 else result

    def _merge_records(self, a: dict, b: dict) -> dict:
        fa = {f["name"]: f["type"] for f in a["fields"]}
        fb = {f["name"]: f["type"] for f in b["fields"]}
        fields = []
        for name in list(fa) + [n for n in fb if n not in fa]:
            if name in fa and name in fb:
                typ = self.merge([fa[name], fb[name]])
            else:
                typ = self.merge(["null", fa.get(name, fb.get(name))])
            fields.append(self._field(name, typ))
        return {"type": "record", "name": a["name"], "fields": fields}


def enc_avro(data: Any) -> bytes:
    records = data if isinstance(data, list) else [data]
    inferrer = _AvroInferrer()
    schema = inferrer.merge([inferrer.infer(r) for r in records] or ["null"])
    buf = io.BytesIO()
    try:
        fastavro.writer(buf, fastavro.parse_schema(schema), records)
    except Exception as exc:
        raise ConversionError(f"Data cannot be written as Avro: {exc}.")
    return buf.getvalue()


def dec_avro(raw: bytes) -> Any:
    try:
        return list(fastavro.reader(io.BytesIO(raw)))
    except Exception as exc:
        raise InvalidInputError(
            f"Invalid Avro data: {exc or type(exc).__name__}.",
            hint="Expected an Avro Object Container File (it embeds its own schema).",
        )


# --------------------------------------------------------------------------- FlatBuffers (FlexBuffers)
def dec_flatbuffers(raw: bytes) -> Any:
    try:
        return flexbuffers.Loads(raw)
    except Exception as exc:
        raise InvalidInputError(
            f"Invalid FlatBuffers (FlexBuffers) data: {exc or type(exc).__name__}.",
            hint="This API uses FlexBuffers, FlatBuffers' schema-less format.",
        )


def enc_flatbuffers(data: Any) -> bytes:
    def check(n: int) -> None:
        if not INT64_MIN <= n <= INT64_MAX:
            raise ConversionError(f"Integer {n} does not fit FlexBuffers' 64-bit integer range.", hint="Send very large integers as strings.")

    _walk_ints(data, check)
    try:
        return bytes(flexbuffers.Dumps(data))
    except Exception as exc:
        raise ConversionError(f"Data cannot be written as FlatBuffers (FlexBuffers): {exc}.")


# --------------------------------------------------------------------------- registry
@dataclass(frozen=True)
class Format:
    key: str                # used in URLs, e.g. "messagepack"
    name: str               # display name
    binary: bool
    media_type: str
    extension: str
    decode: Callable[..., Any]
    encode: Callable[..., bytes]
    sample: Optional[str] = None   # example input; for binary formats, JSON for the data the example encodes


FORMAT_NOTES: Dict[str, str] = {
    "flatbuffers": "FlatBuffers normally needs a compiled schema; this API uses FlexBuffers, its schema-less variant.",
    "protobuf": "Protobuf bytes don't contain field names. Pass the .proto text as `proto_schema` (and `proto_message` "
                "if it defines several messages) to read/write real messages with field names. Without a schema, fields "
                "are keyed by number like `protoc --decode_raw` (`{\"1\": \"Ann\", \"2\": 42}`), with types guessed "
                "from the wire format; writing without a schema needs numeric keys.",
    "prototext": "The readable Protobuf object syntax (`name: \"Ann\" id: 42 address { city: \"Paris\" }`). Always needs "
                 "`proto_schema`; the object is serialized to real Protobuf bytes internally, then converted.",
    "avro": "Avro schema is inferred from the data and embedded in the output; decoding always returns a list of records.",
    "bson": "BSON's top level must be a document; non-object data is wrapped as `{\"data\": ...}`.",
    "csv": "CSV needs a list of objects; nested objects become `parent.child` columns.",
    "xml": "XML attributes appear as `@attr` keys and text as `#text`; all XML values are strings.",
}

FORMATS: Dict[str, Format] = {f.key: f for f in [
    Format("json", "JSON", False, "application/json", "json", dec_json, enc_json,
           '{"name": "Alice", "age": 30, "score": 9.5, "active": true, "tags": ["a", "b"], "address": {"city": "Paris"}}'),
    Format("xml", "XML", False, "application/xml", "xml", dec_xml, enc_xml,
           '<person><name>Alice</name><age>30</age><address><city>Paris</city></address></person>'),
    Format("yaml", "YAML", False, "application/yaml", "yaml", dec_yaml, enc_yaml,
           "name: Alice\nage: 30\nscore: 9.5\nactive: true\ntags:\n  - a\n  - b\naddress:\n  city: Paris\n"),
    Format("csv", "CSV", False, "text/csv", "csv", dec_csv, enc_csv,
           "name,age,city\nAlice,30,Paris\nBob,25,Berlin\n"),
    Format("flatbuffers", "FlatBuffers", True, "application/octet-stream", "fb", dec_flatbuffers, enc_flatbuffers),
    Format("protobuf", "Protocol Buffers", True, "application/x-protobuf", "pb", dec_protobuf, enc_protobuf,
           '{"1": "Alice", "2": 30, "3": 9.5, "4": true, "5": ["a", "b"], "6": {"1": "Paris"}}'),
    Format("prototext", "Protobuf Text", False, "text/plain", "txtpb", dec_prototext, enc_prototext,
           'name: "Alice" age: 30 score: 9.5 active: true tags: "a" tags: "b" address { city: "Paris" }'),
    Format("avro", "Avro", True, "application/avro", "avro", dec_avro, enc_avro),
    Format("messagepack", "MessagePack", True, "application/msgpack", "msgpack", dec_msgpack, enc_msgpack),
    Format("cbor", "CBOR", True, "application/cbor", "cbor", dec_cbor, enc_cbor),
    Format("bson", "BSON", True, "application/bson", "bson", dec_bson, enc_bson),
]}
