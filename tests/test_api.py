import base64
import json

import pytest
from fastapi.testclient import TestClient

from converter.codecs import FORMATS
from converter.main import app
from converter.routes import _example_body

client = TestClient(app, raise_server_exceptions=False)

DATA = {"name": "Alice", "age": 30, "score": 9.5, "active": True, "tags": ["a", "b"],
        "address": {"city": "Paris"}, "nothing": None}
PAIRS = [(s, t) for s in FORMATS for t in FORMATS if s != t]


def convert(src, tgt, body, **kw):
    return client.post(f"/convert/{src}-to-{tgt}", content=body, **kw)


def to_json(src, body):
    r = convert(src, "json", body)
    assert r.status_code == 200, r.text
    return json.loads(r.text)


# --------------------------------------------------------------------------- coverage
def test_ninety_endpoints_exist():
    paths = [p for p in app.openapi()["paths"] if p.startswith("/convert/")]
    assert len(paths) == 90 == len(PAIRS)


@pytest.mark.parametrize("src,tgt", PAIRS)
def test_every_pair_converts_swagger_example(src, tgt):
    r = convert(src, tgt, _example_body(FORMATS[src]))
    assert r.status_code == 200, f"{src}->{tgt}: {r.text}"
    assert r.content


# --------------------------------------------------------------------------- round trips
@pytest.mark.parametrize("fmt", ["yaml", "flatbuffers", "protobuf", "messagepack", "cbor", "bson"])
def test_json_roundtrip_lossless(fmt):
    encoded = convert("json", fmt, json.dumps(DATA)).text
    assert to_json(fmt, encoded) == DATA


def test_avro_roundtrip_returns_record_list():
    encoded = convert("json", "avro", json.dumps(DATA)).text
    assert to_json("avro", encoded) == [DATA]


def test_avro_merges_heterogeneous_records():
    rows = [{"a": 1}, {"a": 2, "b": "x"}]
    encoded = convert("json", "avro", json.dumps(rows)).text
    assert to_json("avro", encoded) == [{"a": 1, "b": None}, {"a": 2, "b": "x"}]


def test_binary_raw_output_and_octet_stream_input():
    r = convert("json", "cbor", json.dumps(DATA), params={"response_format": "raw"})
    assert r.headers["content-type"].startswith("application/cbor")
    assert "converted.cbor" in r.headers["content-disposition"]
    back = convert("cbor", "json", r.content, headers={"content-type": "application/octet-stream"})
    assert json.loads(back.text) == DATA


def test_xml_to_json_and_back():
    xml = '<person id="7"><name>Alice</name></person>'
    assert to_json("xml", xml) == {"person": {"@id": "7", "name": "Alice"}}
    r = convert("json", "xml", json.dumps({"person": {"@id": "7", "name": "Alice"}}))
    assert '<person id="7">' in r.text and r.headers["content-type"].startswith("application/xml")


def test_json_array_to_xml_uses_root_param():
    r = convert("json", "xml", "[1, 2]", params={"xml_root": "numbers"})
    assert "<numbers>" in r.text and r.text.count("<item>") == 2


def test_csv_roundtrip_with_type_inference():
    csv_text = "name,age,ok\nAlice,30,true\nBob,25,false\n"
    r = convert("csv", "json", csv_text, params={"infer_types": "true"})
    assert r.json() == [{"name": "Alice", "age": 30, "ok": True}, {"name": "Bob", "age": 25, "ok": False}]
    assert convert("csv", "json", csv_text).json()[0]["age"] == "30"
    back = convert("json", "csv", r.text)
    assert back.text == "name,age,ok\nAlice,30,true\nBob,25,false\n"


def test_nested_json_flattens_for_csv_and_xml_wrapper_is_unwrapped():
    r = convert("json", "csv", json.dumps([{"a": {"b": 1}, "t": [1, 2]}]))
    assert r.text == 'a.b,t\n1,"[1, 2]"\n'
    xml = "<people><person><n>A</n></person><person><n>B</n></person></people>"
    assert convert("xml", "csv", xml).text == "n\nA\nB\n"


# --------------------------------------------------------------------------- errors
def err(r):
    assert r.headers["content-type"].startswith("application/json")
    return r.json()["error"]


def test_invalid_json_gives_line_and_column():
    r = convert("json", "xml", '{"a": 1,}')
    e = err(r)
    assert r.status_code == 400 and e["code"] == "INVALID_INPUT"
    assert "line 1" in e["message"] and e["source_format"] == "json" and e["target_format"] == "xml"


@pytest.mark.parametrize("src,body", [
    ("xml", "<a><b></a>"), ("yaml", "a: [1, 2"), ("csv", "a,b\n1,2,3\n"),
    ("messagepack", base64.b64encode(b"\xc1").decode()), ("cbor", base64.b64encode(b"\xff\xff").decode()),
    ("bson", base64.b64encode(b"nope").decode()), ("avro", base64.b64encode(b"nope").decode()),
    ("protobuf", base64.b64encode(b"\xff\xff\xff").decode()), ("flatbuffers", base64.b64encode(b"\x01").decode()),
])
def test_malformed_input_is_400_for_every_format(src, body):
    r = convert(src, "json", body)
    assert r.status_code == 400, r.text
    assert err(r)["code"] == "INVALID_INPUT"


def test_empty_body_and_bad_base64_and_bad_utf8():
    assert convert("json", "xml", "").status_code == 400
    r = convert("cbor", "json", "not base64!!")
    assert r.status_code == 400 and "base64" in err(r)["message"]
    r = convert("json", "xml", b"\xff\xfe\xfa")
    assert r.status_code == 400 and "UTF-8" in err(r)["message"]


@pytest.mark.parametrize("tgt,body,fragment", [
    ("xml", '{"bad key": 1}', "valid XML"),
    ("avro", '{"bad-key": 1}', "Avro field name"),
    ("bson", '{"n": 18446744073709551616}', "64-bit"),
    ("protobuf", '{"n": 9007199254740993}', "exactly"),
    ("csv", "[]", "empty"),
    ("csv", "[[1, 2]]", "list of objects"),
    ("json", "NaN", "NaN"),
])
def test_unrepresentable_data_is_422(tgt, body, fragment):
    src = "yaml" if tgt == "json" else "json"
    r = convert(src, tgt, ".nan" if tgt == "json" else body)
    e = err(r)
    assert r.status_code == 422 and e["code"] == "CONVERSION_FAILED" and e["stage"] == "serialize"
    assert fragment in e["message"]


def test_other_errors_use_same_envelope():
    assert err(client.get("/convert/json-to-xml"))["code"] == "METHOD_NOT_ALLOWED"
    assert err(client.post("/convert/json-to-nope", content="{}"))["code"] == "NOT_FOUND"
    r = convert("json", "cbor", "{}", params={"response_format": "zip"})
    assert r.status_code == 422 and err(r)["code"] == "INVALID_PARAMETER"


def test_payload_too_large(monkeypatch):
    monkeypatch.setattr("converter.routes.MAX_BODY_BYTES", 10)
    r = convert("json", "xml", json.dumps(DATA))
    assert r.status_code == 413 and err(r)["code"] == "PAYLOAD_TOO_LARGE"


def test_deeply_nested_input_does_not_crash():
    r = convert("json", "yaml", "[" * 5000 + "]" * 5000)
    assert r.status_code in (400, 422)


def test_swagger_and_utility_routes():
    assert client.get("/docs").status_code == 200
    assert client.get("/health").json() == {"status": "ok"}
    assert len(client.get("/formats").json()["formats"]) == 10
    params = {p["name"] for p in app.openapi()["paths"]["/convert/csv-to-xml"]["post"]["parameters"]}
    assert params == {"infer_types", "pretty", "xml_root"}
