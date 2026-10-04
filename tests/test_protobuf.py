import asyncio
import base64
import json

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from converter.main import app
from converter.mcp_server import mcp

client = TestClient(app, raise_server_exceptions=False)

SCHEMA = """
syntax = "proto3";
package demo;

message Person {
  string name = 1;
  int32 id = 2;
  repeated string tags = 3;
  Address address = 4;
  int64 big = 5;
  double score = 6;
  bool active = 7;
  bytes blob = 8;
}

message Address { string city = 1; }
"""
ONE_MESSAGE = 'syntax = "proto3"; message Person { string name = 1; int32 id = 2; }'

# Person{name: "Ann", id: 42} as any protobuf library serializes it.
PERSON_BYTES = b"\x0a\x03Ann\x10\x2a"
PERSON_B64 = base64.b64encode(PERSON_BYTES).decode()


def convert(src, tgt, body, **params):
    return client.post(f"/convert/{src}-to-{tgt}", content=body, params=params)


def error(r):
    return r.json()["error"]


# --------------------------------------------------------------------------- schema mode
def test_reads_real_protobuf_with_schema():
    r = convert("protobuf", "json", PERSON_B64, proto_schema=SCHEMA, proto_message="Person")
    assert r.status_code == 200, r.text
    assert r.json() == {"name": "Ann", "id": 42}


def test_writes_the_same_bytes_as_protobuf_libraries():
    r = convert("json", "protobuf", '{"name": "Ann", "id": 42}', proto_schema=SCHEMA, proto_message="demo.Person")
    assert base64.b64decode(r.text) == PERSON_BYTES


def test_schema_roundtrip_keeps_names_and_types():
    data = {"name": "Ann", "id": -7, "tags": ["a", "b"], "address": {"city": "Paris"},
            "big": 123456789012, "score": 9.5, "active": True, "blob": base64.b64encode(b"\x00\xff").decode()}
    encoded = convert("json", "protobuf", json.dumps(data), proto_schema=SCHEMA, proto_message="Person").text
    back = convert("protobuf", "json", encoded, proto_schema=SCHEMA, proto_message="Person").json()
    assert back == data


def test_int64_beyond_double_precision_is_kept_exact():
    encoded = convert("json", "protobuf", '{"big": 9007199254740993}', proto_schema=SCHEMA, proto_message="Person").text
    back = convert("protobuf", "json", encoded, proto_schema=SCHEMA, proto_message="Person").json()
    assert int(back["big"]) == 9007199254740993


def test_single_message_schema_needs_no_message_name_and_works_from_yaml():
    encoded = convert("yaml", "protobuf", "name: Ann\nid: 42\n", proto_schema=ONE_MESSAGE).text
    assert base64.b64decode(encoded) == PERSON_BYTES


def test_raw_bytes_in_and_out():
    r = client.post("/convert/json-to-protobuf", content='{"name": "Ann", "id": 42}',
                    params={"proto_schema": ONE_MESSAGE, "response_format": "raw"})
    assert r.content == PERSON_BYTES
    back = client.post("/convert/protobuf-to-json", content=PERSON_BYTES,
                       params={"proto_schema": ONE_MESSAGE}, headers={"content-type": "application/octet-stream"})
    assert back.json() == {"name": "Ann", "id": 42}


@pytest.mark.parametrize("params,fragment", [
    ({"proto_schema": SCHEMA}, "choose one with proto_message"),
    ({"proto_schema": SCHEMA, "proto_message": "Nope"}, "not defined"),
    ({"proto_schema": "message X { strin a = 1; }"}, "schema.proto:1:"),
    ({"proto_message": "Person"}, "without proto_schema"),
])
def test_schema_problems_are_reported_as_invalid_schema(params, fragment):
    r = convert("protobuf", "json", PERSON_B64, **params)
    e = error(r)
    assert r.status_code == 400 and e["code"] == "INVALID_SCHEMA" and e["stage"] == "request"
    assert fragment in e["message"]


def test_schema_errors_are_request_errors_when_writing_too():
    e = error(convert("json", "protobuf", '{"name": "Ann"}', proto_schema=SCHEMA))
    assert e["code"] == "INVALID_SCHEMA" and e["stage"] == "request"


def test_data_that_does_not_match_the_schema_is_rejected_not_dropped():
    r = convert("protobuf", "json", PERSON_B64, proto_schema='syntax = "proto3"; message Other { int32 a = 1; }')
    assert r.status_code == 400 and "does not define" in error(r)["message"]


def test_unknown_field_name_when_writing_is_422():
    r = convert("json", "protobuf", '{"nmae": "Ann"}', proto_schema=ONE_MESSAGE)
    assert r.status_code == 422 and "nmae" in error(r)["message"]


# --------------------------------------------------------------------------- raw mode
def test_reads_real_protobuf_without_schema_by_field_number():
    r = convert("protobuf", "json", PERSON_B64)
    assert r.status_code == 200, r.text
    assert r.json() == {"1": "Ann", "2": 42}


def test_raw_mode_infers_nested_repeated_negative_and_double():
    encoded = convert("json", "protobuf", json.dumps(
        {"name": "Ann", "id": -7, "tags": ["a", "b"], "address": {"city": "Paris"}, "score": 9.5}),
        proto_schema=SCHEMA, proto_message="Person").text
    assert convert("protobuf", "json", encoded).json() == {
        "1": "Ann", "2": -7, "3": ["a", "b"], "4": {"1": "Paris"}, "6": 9.5}


def test_raw_roundtrip_with_numeric_keys():
    data = {"1": "Ann", "2": 42, "3": ["a", "b"], "4": {"1": "Paris"}, "6": 9.5, "7": -1}
    encoded = convert("json", "protobuf", json.dumps(data)).text
    assert convert("protobuf", "json", encoded).json() == data


def test_raw_mode_keeps_text_that_looks_like_a_message_as_text():
    encoded = convert("json", "protobuf", '{"1": "Hello world"}').text
    assert convert("protobuf", "json", encoded).json() == {"1": "Hello world"}


@pytest.mark.parametrize("body", ['["a"]', '{"0": 1}', '{"1": [[1]]}'])
def test_raw_mode_rejects_data_it_cannot_write(body):
    assert convert("json", "protobuf", body).status_code == 422


@pytest.mark.parametrize("raw", [b"\x0a\x05Ann", b"\x0b", b"\x00\x01", b"\xff" * 11])
def test_raw_mode_rejects_malformed_wire_data(raw):
    r = convert("protobuf", "json", base64.b64encode(raw).decode())
    assert r.status_code == 400 and error(r)["code"] == "INVALID_INPUT"


# --------------------------------------------------------------------------- Protobuf text format (message objects)
ORDER_SCHEMA = """
syntax = "proto3";
package shop;
message Order {
  enum Status { PENDING = 0; PAID = 1; SHIPPED = 2; }
  message Item { string sku = 1; int32 quantity = 2; double price = 3; }
  string order_id = 1;
  Status status = 2;
  repeated Item items = 3;
  bool gift = 4;
  int64 created_at = 5;
}
"""
ORDER_TEXT = """
order_id: "ORD-1001"
status: SHIPPED
items { sku: "BOOK-7" quantity: 2 price: 12.5 }
items { sku: "PEN-3" quantity: 10 price: 0.99 }
gift: true  # comments are allowed
created_at: 1791050400
"""
ORDER_JSON = {"order_id": "ORD-1001", "status": "SHIPPED",
              "items": [{"sku": "BOOK-7", "quantity": 2, "price": 12.5}, {"sku": "PEN-3", "quantity": 10, "price": 0.99}],
              "gift": True, "created_at": 1791050400}
# The same Order as encoded by `protoc --encode=shop.Order`.
ORDER_B64 = "CghPUkQtMTAwMRACGhMKBkJPT0stNxACGQAAAAAAAClAGhIKBVBFTi0zEAoZrkfhehSu7z8gASighYXWBg=="


def test_message_object_to_json():
    r = convert("prototext", "json", ORDER_TEXT, proto_schema=ORDER_SCHEMA)
    assert r.status_code == 200, r.text
    assert r.json() == ORDER_JSON


def test_message_object_serializes_to_the_same_bytes_as_protoc():
    r = convert("prototext", "protobuf", ORDER_TEXT, proto_schema=ORDER_SCHEMA)
    assert r.text == ORDER_B64


def test_json_and_bytes_back_to_message_object():
    text = convert("json", "prototext", json.dumps(ORDER_JSON), proto_schema=ORDER_SCHEMA).text
    assert 'status: SHIPPED' in text and 'items {' in text
    assert convert("prototext", "json", text, proto_schema=ORDER_SCHEMA).json() == ORDER_JSON
    one_line = convert("protobuf", "prototext", ORDER_B64, proto_schema=ORDER_SCHEMA, pretty=False).text
    assert "\n" not in one_line.strip()
    assert convert("prototext", "json", one_line, proto_schema=ORDER_SCHEMA).json() == ORDER_JSON


def test_message_object_errors():
    e = error(convert("prototext", "json", 'order_id: "X" stauts: PAID', proto_schema=ORDER_SCHEMA))
    assert e["code"] == "INVALID_INPUT" and "stauts" in e["message"] and "1:" in e["message"]
    e = error(convert("prototext", "json", 'order_id: "X"'))
    assert e["code"] == "INVALID_SCHEMA" and e["stage"] == "request"
    e = error(convert("json", "prototext", '{"order_id": "X"}'))
    assert e["code"] == "INVALID_SCHEMA"


# --------------------------------------------------------------------------- MCP
def call(tool, args):
    async def run():
        async with Client(mcp) as c:
            return await c.call_tool(tool, args)
    return asyncio.run(run())


def test_mcp_convert_with_schema_both_ways():
    enc = call("convert_data", {"source_format": "json", "target_format": "protobuf",
                                "data": '{"name": "Ann", "id": 42}', "proto_schema": ONE_MESSAGE}).structured_content
    assert base64.b64decode(enc["output"]) == PERSON_BYTES
    back = call("convert_data", {"source_format": "protobuf", "target_format": "json", "data": PERSON_B64,
                                 "proto_schema": SCHEMA, "proto_message": "Person"}).structured_content
    assert json.loads(back["output"]) == {"name": "Ann", "id": 42}


def test_mcp_validate_with_and_without_schema():
    raw = call("validate_data", {"format": "protobuf", "data": PERSON_B64}).structured_content
    assert raw["valid"] is True
    wrong = call("validate_data", {"format": "protobuf", "data": PERSON_B64,
                                   "proto_schema": 'syntax = "proto3"; message Other { int32 a = 1; }'}).structured_content
    assert wrong["valid"] is False and "does not define" in wrong["error"]["message"]


def test_mcp_converts_message_object_with_alias():
    r = call("convert_data", {"source_format": "textproto", "target_format": "json", "data": ORDER_TEXT,
                              "proto_schema": ORDER_SCHEMA})
    assert not r.is_error and json.loads(r.structured_content["output"]) == ORDER_JSON


def test_mcp_tool_schema_advertises_proto_options():
    async def run():
        async with Client(mcp) as c:
            return {t.name: t for t in (await c.list_tools()).tools}
    tools = asyncio.run(run())
    for name in ("convert_data", "validate_data"):
        assert {"proto_schema", "proto_message"} <= set(tools[name].input_schema["properties"])
