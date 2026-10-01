import asyncio
import base64
import json

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from converter import mcp_server
from converter.codecs import FORMATS
from converter.main import app
from converter.mcp_server import mcp

DATA = {"name": "Alice", "age": 30, "tags": ["a", "b"], "address": {"city": "Paris"}, "nothing": None}


def call(tool, args):
    """Call a tool through a real in-memory MCP client session."""
    async def run():
        async with Client(mcp) as client:
            return await client.call_tool(tool, args)
    return asyncio.run(run())


def error_of(result):
    assert result.is_error, "expected an isError tool result"
    text = result.content[0].text
    return json.loads(text[text.index("{"):])["error"]


# --------------------------------------------------------------------------- discovery
def test_lists_three_tools_with_format_enums():
    async def run():
        async with Client(mcp) as client:
            return (await client.list_tools()).tools
    tools = {t.name: t for t in asyncio.run(run())}
    assert set(tools) == {"convert_data", "validate_data", "list_formats"}
    assert tools["convert_data"].input_schema["properties"]["source_format"]["enum"] == list(FORMATS)
    assert tools["convert_data"].annotations.read_only_hint is True


def test_list_formats_covers_all_formats():
    r = call("list_formats", {})
    assert not r.is_error
    assert [f["key"] for f in r.structured_content["formats"]] == list(FORMATS)


# --------------------------------------------------------------------------- conversions
def test_json_to_xml_text_output():
    r = call("convert_data", {"source_format": "json", "target_format": "xml", "data": '{"a": {"b": 1}}'})
    out = r.structured_content
    assert not r.is_error and out["encoding"] == "text" and "<b>1</b>" in out["output"]


@pytest.mark.parametrize("fmt", ["messagepack", "cbor", "bson", "protobuf", "flatbuffers"])
def test_binary_roundtrip_is_base64_both_ways(fmt):
    enc = call("convert_data", {"source_format": "json", "target_format": fmt, "data": json.dumps(DATA)}).structured_content
    assert enc["encoding"] == "base64"
    base64.b64decode(enc["output"], validate=True)
    back = call("convert_data", {"source_format": fmt, "target_format": "json", "data": enc["output"]}).structured_content
    assert json.loads(back["output"]) == DATA


def test_aliases_and_options():
    r = call("convert_data", {"source_format": "MsgPack", "target_format": "YML",
                              "data": call("convert_data", {"source_format": "json", "target_format": "msgpack",
                                                           "data": '{"a": 1}'}).structured_content["output"]})
    assert not r.is_error and r.structured_content["output"].strip() == "a: 1"
    csv = call("convert_data", {"source_format": "csv", "target_format": "json", "data": "n,age\nA,30\n", "infer_types": True})
    assert json.loads(csv.structured_content["output"]) == [{"n": "A", "age": 30}]


# --------------------------------------------------------------------------- validate_data
def test_validate_data_reports_instead_of_failing():
    ok = call("validate_data", {"format": "json", "data": '{"a": [1, 2]}'}).structured_content
    assert ok["valid"] is True and ok["summary"] == "object with 1 key(s)"
    bad = call("validate_data", {"format": "yaml", "data": "a: [1, 2"})
    assert not bad.is_error and bad.structured_content["valid"] is False
    assert bad.structured_content["error"]["code"] == "INVALID_INPUT"
    assert call("validate_data", {"format": "cbor", "data": "!!!"}).structured_content["valid"] is False
    assert call("validate_data", {"format": "json", "data": "  "}).structured_content["valid"] is False


# --------------------------------------------------------------------------- errors
def test_unknown_format_suggests_correction():
    e = error_of(call("convert_data", {"source_format": "jsn", "target_format": "xml", "data": "{}"}))
    assert e["code"] == "INVALID_FORMAT" and "Did you mean 'json'" in e["message"] and "Supported formats" in e["hint"]


def test_invalid_input_has_location_and_formats():
    e = error_of(call("convert_data", {"source_format": "json", "target_format": "xml", "data": '{"a": 1,}'}))
    assert e["code"] == "INVALID_INPUT" and "line 1" in e["message"]
    assert (e["source_format"], e["target_format"], e["stage"]) == ("json", "xml", "parse")


@pytest.mark.parametrize("args,code", [
    ({"source_format": "json", "target_format": "xml", "data": ""}, "INVALID_INPUT"),
    ({"source_format": "cbor", "target_format": "json", "data": "not base64!!"}, "INVALID_INPUT"),
    ({"source_format": "json", "target_format": "xml", "data": '{"bad key": 1}'}, "CONVERSION_FAILED"),
    ({"source_format": "json", "target_format": "bson", "data": '{"n": 18446744073709551616}'}, "CONVERSION_FAILED"),
    ({"source_format": "json", "target_format": "avro", "data": '{"bad-key": 1}'}, "CONVERSION_FAILED"),
])
def test_domain_errors_are_tool_errors(args, code):
    e = error_of(call("convert_data", args))
    assert e["code"] == code and e.get("hint")
    if code == "CONVERSION_FAILED":
        assert e["stage"] == "serialize"


def test_wrong_argument_types_are_rejected():
    assert call("convert_data", {"source_format": "json", "target_format": "xml", "data": 123}).is_error
    assert call("convert_data", {"source_format": "json"}).is_error  # missing arguments


def test_input_and_output_limits(monkeypatch):
    monkeypatch.setattr(mcp_server, "MAX_INPUT_BYTES", 10)
    assert error_of(call("convert_data", {"source_format": "json", "target_format": "xml", "data": json.dumps(DATA)}))["code"] == "PAYLOAD_TOO_LARGE"
    monkeypatch.setattr(mcp_server, "MAX_INPUT_BYTES", 10_000)
    monkeypatch.setattr(mcp_server, "MAX_OUTPUT_BYTES", 10)
    assert error_of(call("convert_data", {"source_format": "json", "target_format": "xml", "data": json.dumps(DATA)}))["code"] == "OUTPUT_TOO_LARGE"


def test_unexpected_exception_is_generic_and_server_survives(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret internal detail")
    monkeypatch.setattr(mcp_server, "convert", boom)

    async def run():
        async with Client(mcp) as client:
            first = await client.call_tool("convert_data", {"source_format": "json", "target_format": "xml", "data": "{}"})
            second = await client.call_tool("list_formats", {})
            return first, second
    first, second = asyncio.run(run())
    e = error_of(first)
    assert e["code"] == "INTERNAL_ERROR" and "secret" not in json.dumps(e)
    assert not second.is_error  # same session still works after a crash


def test_deeply_nested_input_does_not_crash():
    r = call("convert_data", {"source_format": "json", "target_format": "yaml", "data": "[" * 5000 + "]" * 5000})
    assert error_of(r)["code"] in ("INVALID_INPUT", "CONVERSION_FAILED")


# --------------------------------------------------------------------------- HTTP transport at /mcp
HEADERS = {"content-type": "application/json", "accept": "application/json, text/event-stream"}


def rpc(client, method, params=None, id=1):
    return client.post("/mcp", headers=HEADERS, json={"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}})


def test_http_endpoint_serves_tools_and_does_not_break_rest():
    with TestClient(app) as client:  # note: no lifespan dependency in MCPHttpApp
        init = rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                          "clientInfo": {"name": "t", "version": "1"}})
        assert init.status_code == 200 and init.json()["result"]["serverInfo"]["name"] == "format-converter"
        tools = rpc(client, "tools/list", id=2).json()["result"]["tools"]
        assert {t["name"] for t in tools} == {"convert_data", "validate_data", "list_formats"}
        res = rpc(client, "tools/call", {"name": "convert_data", "arguments": {
            "source_format": "yaml", "target_format": "json", "data": "a: 1"}}, id=3).json()["result"]
        assert res["isError"] is False and json.loads(res["structuredContent"]["output"]) == {"a": 1}
        bad = rpc(client, "tools/call", {"name": "convert_data", "arguments": {
            "source_format": "json", "target_format": "xml", "data": "{"}}, id=4).json()["result"]
        assert bad["isError"] is True
        # several requests in a row work (a fresh stateless session each time)
        assert rpc(client, "tools/list", id=5).status_code == 200
        assert client.get("/health").json() == {"status": "ok"}
        assert client.post("/convert/json-to-xml", content='{"a":1}').status_code == 200


def test_http_garbage_gets_a_protocol_error_not_a_crash():
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/mcp", headers=HEADERS, content="this is not json-rpc")
    assert r.status_code in (400, 422) and r.status_code != 500
