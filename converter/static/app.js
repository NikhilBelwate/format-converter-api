(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const source = $("source"), target = $("target"), input = $("input"), output = $("output");
  const status = $("status"), hint = $("hint"), convertBtn = $("convert");
  const formats = {};
  const PROTO = ["protobuf", "prototext"];
  const EXAMPLE_SCHEMA = [
    'syntax = "proto3";',
    "message Person {",
    "  message Address { string city = 1; }",
    "  string name = 1;",
    "  int32 age = 2;",
    "  double score = 3;",
    "  bool active = 4;",
    "  repeated string tags = 5;",
    "  Address address = 6;",
    "}",
  ].join("\n");
  // XML always has a root element, which Protobuf sees as a single field, so the XML example wraps Person in a message.
  const EXAMPLE_SCHEMA_XML = EXAMPLE_SCHEMA.replace("\nmessage Person {", "\nmessage Doc { Person person = 1; }\nmessage Person {");
  const exampleSchema = () => (source.value === "xml" ? EXAMPLE_SCHEMA_XML : EXAMPLE_SCHEMA);
  const exampleMessage = () => (source.value === "xml" ? "Doc" : "Person");
  const EXAMPLE_DATA = {
    json: '{\n  "name": "Alice",\n  "age": 30,\n  "score": 9.5,\n  "active": true,\n  "tags": ["a", "b"],\n  "address": { "city": "Paris" }\n}',
    xml: "<person>\n  <name>Alice</name>\n  <age>30</age>\n  <score>9.5</score>\n  <tags>a</tags>\n  <tags>b</tags>\n  <address>\n    <city>Paris</city>\n  </address>\n</person>",
    yaml: "name: Alice\nage: 30\nscore: 9.5\nactive: true\ntags: [a, b]\naddress:\n  city: Paris\n",
    prototext: 'name: "Alice"\nage: 30\nscore: 9.5\nactive: true\ntags: "a"\ntags: "b"\naddress { city: "Paris" }',
  };

  function setStatus(text, kind) {
    status.textContent = text || "";
    status.className = kind || "";
  }

  function updateUi() {
    const s = formats[source.value], t = formats[target.value];
    if (!s || !t) return;
    const notes = [];
    if (s.binary) notes.push(s.name + " input must be base64 text.");
    if (t.binary) notes.push(t.name + " output is shown as base64 text.");
    hint.textContent = notes.join(" ");
    const needsSchema = PROTO.includes(source.value) || PROTO.includes(target.value);
    $("schema-box").hidden = !needsSchema;
    $("schema-box").classList.remove("attention");
    if (needsSchema && !$("schema").value.trim()) {
      $("schema").value = exampleSchema();
      $("message").value = exampleMessage();
    }
    if (source.value === "xml" && PROTO.includes(target.value)) {
      notes.push("The XML root element is treated as a Protobuf field name (the example wraps it in a Doc message). XML has no types, so boolean fields and single-item lists cannot be converted from XML; use JSON or YAML for those.");
      hint.textContent = notes.join(" ");
    }
  }

  function loadExample() {
    $("schema").value = exampleSchema();
    $("message").value = exampleMessage();
    const sample = EXAMPLE_DATA[source.value];
    if (sample) input.value = sample;
    setStatus(sample ? "Loaded the example schema and sample data." : "Loaded the example schema.", "ok");
  }

  function fill(select, selected) {
    select.innerHTML = "";
    Object.values(formats).forEach((f) => {
      const o = document.createElement("option");
      o.value = f.key;
      o.textContent = f.name;
      select.appendChild(o);
    });
    select.value = selected;
  }

  async function load() {
    try {
      const res = await fetch("/formats");
      const data = await res.json();
      data.formats.forEach((f) => { formats[f.key] = f; });
      const keys = Object.keys(formats);
      fill(source, formats.json ? "json" : keys[0]);
      fill(target, formats.xml ? "xml" : keys[1]);
      updateUi();
    } catch (e) {
      setStatus("Could not load the list of formats: " + e.message, "error");
    }
  }

  async function convert() {
    if (source.value === target.value) {
      output.value = input.value;
      setStatus("Source and target are the same format; nothing to convert.", "ok");
      return;
    }
    if (!input.value.trim()) {
      setStatus("Enter some input data first.", "error");
      return;
    }
    const params = new URLSearchParams();
    if (!$("schema-box").hidden) {
      if ($("schema").value.trim()) params.set("proto_schema", $("schema").value);
      if ($("message").value.trim()) params.set("proto_message", $("message").value.trim());
    }
    const qs = params.toString() ? "?" + params.toString() : "";
    convertBtn.disabled = true;
    setStatus("Converting...");
    try {
      const res = await fetch("/convert/" + source.value + "-to-" + target.value + qs, {
        method: "POST",
        headers: { "Content-Type": "text/plain; charset=utf-8" },
        body: input.value,
      });
      const text = await res.text();
      if (res.ok) {
        output.value = text;
        setStatus("Converted " + formats[source.value].name + " to " + formats[target.value].name + ".", "ok");
      } else {
        output.value = "";
        let msg = text;
        try {
          const err = JSON.parse(text).error;
          msg = err.message + (err.hint ? "\nHint: " + err.hint : "");
        } catch (_) { /* not JSON */ }
        setStatus(msg, "error");
        if (!$("schema-box").hidden && /CONVERSION_FAILED|INVALID_SCHEMA/.test(text)) {
          $("schema-box").classList.add("attention");
          $("schema-box").scrollIntoView({ behavior: "smooth", block: "center" });
        }
      }
    } catch (e) {
      setStatus("Request failed: " + e.message, "error");
    } finally {
      convertBtn.disabled = false;
    }
  }

  convertBtn.addEventListener("click", convert);
  source.addEventListener("change", updateUi);
  target.addEventListener("change", updateUi);
  $("swap").addEventListener("click", () => {
    const s = source.value;
    source.value = target.value;
    target.value = s;
    if (output.value) { input.value = output.value; output.value = ""; }
    updateUi();
  });
  $("example").addEventListener("click", loadExample);
  $("copy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(output.value); setStatus("Copied to clipboard.", "ok"); }
    catch (_) { output.select(); document.execCommand("copy"); }
  });
  load();
})();
