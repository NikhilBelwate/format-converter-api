"""Reduce whatever a decoder returns to plain JSON-like Python values.

Every conversion goes source -> plain data -> target, so encoders only ever see
None, bool, int, float, str, list and dict (with str keys).
"""
import base64
import datetime as dt
import decimal
import uuid
from typing import Any

MAX_DEPTH = 200


def to_plain(obj: Any, _depth: int = 0) -> Any:
    from .errors import InvalidInputError

    if _depth > MAX_DEPTH:
        raise InvalidInputError(f"Data is nested more than {MAX_DEPTH} levels deep.")
    if obj is None or isinstance(obj, (bool, str)):
        return obj
    if isinstance(obj, int):
        return int(obj)
    if isinstance(obj, float):
        return float(obj)
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(obj)).decode("ascii")
    if isinstance(obj, dict):
        return {_key(k): to_plain(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [to_plain(v, _depth + 1) for v in obj]
    if isinstance(obj, (dt.datetime, dt.date, dt.time)):
        return obj.isoformat()
    if isinstance(obj, decimal.Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    if isinstance(obj, uuid.UUID):
        return str(obj)
    value = getattr(obj, "value", None)  # e.g. cbor2.CBORTag
    if type(obj).__name__ == "CBORTag":
        return to_plain(value, _depth + 1)
    if type(obj).__name__ == "CBORSimpleValue" or type(obj).__name__ == "UndefinedType":
        return None
    return str(obj)  # ObjectId, Decimal128, Regex, ... -> readable string


def _key(k: Any) -> str:
    if isinstance(k, str):
        return k
    if isinstance(k, bool):
        return "true" if k else "false"
    if isinstance(k, (bytes, bytearray)):
        return base64.b64encode(bytes(k)).decode("ascii")
    return str(k)
