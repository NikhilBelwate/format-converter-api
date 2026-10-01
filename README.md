# Format Converter API

FastAPI service that converts data between **JSON, XML, YAML, CSV, FlatBuffers, Protocol Buffers, Avro, MessagePack, CBOR and BSON**.
All 90 directional pairs are exposed as `POST /convert/{source}-to-{target}`, with Swagger UI at `/docs`.

## Run locally

```bash
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn api.index:app --reload   # http://127.0.0.1:8000/docs
pytest
```

## Deploy to Vercel

```bash
npm i -g vercel
vercel          # preview
vercel --prod   # production
```

`api/index.py` is the entrypoint and `vercel.json` rewrites every path to it, so `/docs`, `/formats` and `/convert/...` all work.

## Using the API

Endpoint names: `json`, `xml`, `yaml`, `csv`, `flatbuffers`, `protobuf`, `avro`, `messagepack`, `cbor`, `bson`.
Examples: `/convert/json-to-xml`, `/convert/csv-to-bson`, `/convert/cbor-to-yaml`. `GET /formats` lists them all.

Send the source document as the **raw request body**:

```bash
curl -X POST localhost:8000/convert/json-to-xml -d '{"user": {"name": "Ann"}}'
curl -X POST "localhost:8000/convert/json-to-messagepack"  -d '{"a": 1}'          # -> gaFhAQ== (base64)
curl -X POST "localhost:8000/convert/messagepack-to-json" -d 'gaFhAQ=='           # -> {"a": 1}
curl -X POST "localhost:8000/convert/json-to-cbor?response_format=raw" -d '{"a":1}' -o out.cbor
curl -X POST localhost:8000/convert/cbor-to-json -H "Content-Type: application/octet-stream" --data-binary @out.cbor
```

| Kind | Formats | Request body | Response |
|---|---|---|---|
| Text | JSON, XML, YAML, CSV | plain text | plain text |
| Binary | FlatBuffers, Protobuf, Avro, MessagePack, CBOR, BSON | base64 text, or raw bytes with `Content-Type: application/octet-stream` | base64 text, or raw bytes with `?response_format=raw` |

Optional query parameters (shown in Swagger only where relevant): `pretty` (JSON/XML/YAML output), `xml_root` (XML output),
`infer_types` (CSV input → numbers/booleans), `response_format` (binary output).

## Errors

Every failure uses one envelope:

```json
{"error": {"code": "INVALID_INPUT", "message": "Invalid JSON: Expecting value (line 1, column 9).",
           "hint": "...", "source_format": "json", "target_format": "xml", "stage": "parse"}}
```

| Status | `code` | Meaning |
|---|---|---|
| 400 | `INVALID_INPUT` | Empty body, bad base64/UTF-8, or the body isn't valid source-format data (message includes line/column when available) |
| 422 | `CONVERSION_FAILED` | Input is valid but can't be expressed in the target (e.g. XML-illegal key names, integer too large for BSON, CSV from non-tabular data) |
| 422 | `INVALID_PARAMETER` | Bad query parameter |
| 413 | `PAYLOAD_TOO_LARGE` | Body over 4 MB (`MAX_BODY_BYTES` env var; Vercel itself caps at 4.5 MB) |
| 404 / 405 | `NOT_FOUND` / `METHOD_NOT_ALLOWED` | Unknown endpoint / wrong HTTP method |
| 500 | `INTERNAL_ERROR` | Unexpected; details are logged, not returned |

## Format semantics worth knowing

Every conversion goes *source → plain JSON-like data → target*, so these format quirks apply:

- **FlatBuffers** normally needs a compiled schema. This API uses **FlexBuffers**, FlatBuffers' schema-less format.
- **Protobuf** is encoded as a `google.protobuf.Value` message (schema-less). Numbers are doubles: integers beyond ±2^53 are rejected, and integral doubles decode back as ints.
- **Avro** schema is inferred from the data and embedded in the output (Object Container File). Records with different keys are merged (missing keys become nullable). Decoding always returns a **list** of records. Keys must be valid Avro names (`[A-Za-z_][A-Za-z0-9_]*`).
- **BSON** needs a document at the top level; other data is wrapped as `{"data": ...}`.
- **XML**: attributes are `@name`, text with attributes is `#text`; all values are strings. Data without a single root (e.g. arrays) is wrapped in `<root>` (configurable) and array items become `<item>`.
- **CSV** needs a list of objects. Nested objects flatten to `parent.child` columns; arrays become JSON strings. Single-key wrappers (like an XML root) are looked through to find the rows. Values are strings unless `infer_types=true`.
- Binary values coming from MessagePack/CBOR/BSON become base64 strings; dates become ISO-8601 strings.
- YAML: single document, safe-loaded (no arbitrary Python objects).

## Layout

```
api/index.py          Vercel entrypoint
converter/codecs.py   every format's decoder/encoder + registry
converter/routes.py   the 90 generated, explicitly named endpoints
converter/main.py     app, Swagger metadata, error handlers
converter/errors.py   exception types + error response model
tests/                pytest suite (all 90 endpoints, round trips, error cases)
```
