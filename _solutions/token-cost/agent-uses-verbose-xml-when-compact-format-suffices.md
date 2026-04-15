---
layout: solution
title: "Agent uses verbose XML when compact format suffices"
category: token-cost
description: "Agent wraps every response in heavyweight XML tags with repeated attribute names, consuming 3–10x more tokens than a compact JSON or plain-text equivalent."
tags: [token-cost, xml, format, prompt-engineering, output]
---

## Symptom

The agent's output is wrapped in deeply nested XML with redundant attributes, namespaces, and closing tags that carry no additional information:

```xml
<response>
  <analysis>
    <item id="1" type="finding" priority="high">
      <title>Authentication missing</title>
      <description>The endpoint lacks authentication</description>
    </item>
  </analysis>
</response>
```

A compact equivalent carries the same information in 60% fewer tokens:

```json
{"findings": [{"id": 1, "priority": "high", "title": "Authentication missing", "description": "The endpoint lacks authentication"}]}
```

## Root Cause

XML is verbose by design — every value requires an opening and closing tag, and attributes repeat the key name in every row. When a system prompt instructs the model to respond in XML without specifying a compact schema, the model defaults to human-readable, well-indented XML. The whitespace, tag repetition, and attribute overhead compounds across many fields and many records.

## Fix

Replace XML with a compact alternative suited to downstream parsing needs: minified JSON for machine consumption, delimited plain text for human reading, or a hybrid schema with compact tags for mixed audiences.

---

### Option 1 — Replace XML output instruction with compact JSON

```python
import anthropic
import json

client = anthropic.Anthropic()

# Before: XML instruction (verbose)
XML_SYSTEM = """
Analyze the code and respond in XML:
<analysis>
  <issues>
    <issue>
      <severity>high|medium|low</severity>
      <category>string</category>
      <line>integer</line>
      <message>string</message>
    </issue>
  </issues>
</analysis>
"""

# After: compact JSON instruction (same information, fewer tokens)
JSON_SYSTEM = """
Analyze the code. Respond with ONLY minified JSON (no spaces, no newlines):
{"issues":[{"sev":"high|medium|low","cat":"string","line":integer,"msg":"string"}]}
"""

CODE_SAMPLE = """
def login(user, password):
    query = f"SELECT * FROM users WHERE user='{user}'"
    result = db.execute(query)
    token = str(random.random())
    return token
"""

def analyze_xml(code: str) -> tuple[str, int]:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=XML_SYSTEM,
        messages=[{"role": "user", "content": f"Analyze:\n{code}"}],
    )
    text = resp.content[0].text
    return text, resp.usage.output_tokens

def analyze_json(code: str) -> tuple[dict, int]:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=JSON_SYSTEM,
        messages=[{"role": "user", "content": f"Analyze:\n{code}"}],
    )
    text = resp.content[0].text.strip()
    # Strip any markdown fences if present
    if text.startswith("```"):
        text = text.split("```")[1].lstrip("json").strip()
    result = json.loads(text)
    return result, resp.usage.output_tokens

xml_out, xml_tokens  = analyze_xml(CODE_SAMPLE)
json_out, json_tokens = analyze_json(CODE_SAMPLE)

print(f"XML  output: {xml_tokens} tokens")
print(f"JSON output: {json_tokens} tokens")
print(f"Savings: {xml_tokens - json_tokens} tokens ({(1 - json_tokens/xml_tokens)*100:.0f}%)")
print("\nJSON result:")
print(json.dumps(json_out, indent=2))
```

**Expected Token Savings:** 40–65% output token reduction; JSON with short field names is the most token-efficient structured format for machine-readable outputs.

**Environment:** Any agent producing structured data for programmatic consumption; JSON is universally parseable with no additional library.

---

### Option 2 — Short-tag XML schema to retain XML while halving tokens

```python
import anthropic
import xml.etree.ElementTree as ET

client = anthropic.Anthropic()

# Verbose XML schema (long tag names)
VERBOSE_XML_SYSTEM = """
Respond ONLY in XML:
<results>
  <recommendation>
    <priority>high|medium|low</priority>
    <category>performance|security|reliability</category>
    <description>string</description>
    <estimated_effort>hours</estimated_effort>
  </recommendation>
</results>
"""

# Compact XML schema (1-3 char tag names)
COMPACT_XML_SYSTEM = """
Respond ONLY in compact XML (no spaces, no indentation):
<rs><r><p>h|m|l</p><c>perf|sec|rel</c><d>text</d><e>hours</e></r></rs>

Legend: rs=results, r=recommendation, p=priority(h/m/l), c=category, d=description, e=effort_hours
"""

SCENARIO = "Review a Python web app with 50ms database queries and no input validation on login."

def call(system: str) -> tuple[str, int]:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": SCENARIO}],
    )
    return resp.content[0].text, resp.usage.output_tokens

verbose_out, verbose_tok = call(VERBOSE_XML_SYSTEM)
compact_out, compact_tok = call(COMPACT_XML_SYSTEM)

print(f"Verbose XML: {verbose_tok} tokens")
print(f"Compact XML: {compact_tok} tokens")
print(f"Savings: {verbose_tok - compact_tok} tokens ({(1 - compact_tok/verbose_tok)*100:.0f}%)")

# Parse compact XML
try:
    root = ET.fromstring(compact_out.strip())
    print("\nParsed recommendations:")
    priority_map = {"h": "high", "m": "medium", "l": "low"}
    cat_map = {"perf": "performance", "sec": "security", "rel": "reliability"}
    for r in root.findall("r"):
        p = priority_map.get(r.findtext("p", ""), r.findtext("p", ""))
        c = cat_map.get(r.findtext("c", ""), r.findtext("c", ""))
        print(f"  [{p.upper()}] ({c}) {r.findtext('d', '')} — {r.findtext('e', '?')}h")
except ET.ParseError as e:
    print(f"Parse error: {e}\nRaw: {compact_out}")
```

**Expected Token Savings:** 35–55% reduction versus verbose XML while preserving XML structure for teams with XML-only downstream parsers; 1–3 character tags eliminate most tag overhead.

**Environment:** XML-required pipelines (SOAP APIs, legacy systems, XSLT transforms); short tags are a drop-in replacement with no parser changes needed.

---

### Option 3 — CSV output for tabular data

```python
import anthropic
import csv
import io

client = anthropic.Anthropic()

# XML for tabular data (very wasteful)
XML_SYSTEM = """
Return a table of the top 5 cloud providers with pricing in XML:
<providers>
  <provider>
    <name>string</name>
    <tier>string</tier>
    <vcpus>integer</vcpus>
    <ram_gb>integer</ram_gb>
    <price_usd_hr>float</price_usd_hr>
    <region>string</region>
  </provider>
</providers>
"""

# CSV for tabular data (compact)
CSV_SYSTEM = """
Return a CSV table of top 5 cloud VM offerings (no markdown, header row first):
name,tier,vcpus,ram_gb,price_usd_hr,region
"""

def call(system: str, user: str) -> tuple[str, int]:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return resp.content[0].text.strip(), resp.usage.output_tokens

USER = "Give me 5 mid-range VM options for a web app workload."

xml_out, xml_tok = call(XML_SYSTEM, USER)
csv_out, csv_tok = call(CSV_SYSTEM, USER)

print(f"XML tokens: {xml_tok}")
print(f"CSV tokens: {csv_tok}")
print(f"Savings: {xml_tok - csv_tok} tokens ({(1 - csv_tok/xml_tok)*100:.0f}%)")

# Parse CSV
print("\nParsed CSV table:")
reader = csv.DictReader(io.StringIO(csv_out))
for row in reader:
    print(f"  {row.get('name','?'):20} {row.get('tier','?'):10} "
          f"vCPUs={row.get('vcpus','?')} RAM={row.get('ram_gb','?')}GB "
          f"${row.get('price_usd_hr','?')}/hr")
```

**Expected Token Savings:** 50–75% versus XML for tabular data; CSV has zero tag overhead and is directly consumable by pandas, spreadsheets, and database importers.

**Environment:** Reporting agents, data extraction pipelines, any task producing rows of uniform structured data.

---

### Option 4 — Key-value line format for configuration-style outputs

```python
import anthropic

client = anthropic.Anthropic()

# XML for config output
XML_SYSTEM = """
Output the recommended configuration in XML:
<config>
  <setting>
    <key>string</key>
    <value>string</value>
    <reason>string</reason>
  </setting>
</config>
"""

# Key=value format (like .env or .properties)
KV_SYSTEM = """
Output recommended config as KEY=VALUE pairs, one per line.
Append # reason after each value. No XML, no JSON, no markdown.
Example:
MAX_WORKERS=4  # prevents thread exhaustion
"""

SCENARIO = "Recommend Python async web server settings for a 100 req/sec API."

def call(system: str) -> tuple[str, int]:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": SCENARIO}],
    )
    return resp.content[0].text.strip(), resp.usage.output_tokens

xml_out,  xml_tok  = call(XML_SYSTEM)
kv_out,   kv_tok   = call(KV_SYSTEM)

print(f"XML tokens: {xml_tok}")
print(f"KV  tokens: {kv_tok}")
print(f"Savings: {xml_tok - kv_tok} tokens ({(1 - kv_tok/xml_tok)*100:.0f}%)")

# Parse KV format
print("\nParsed config:")
config = {}
for line in kv_out.splitlines():
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    key_val, _, reason = line.partition("#")
    if "=" in key_val:
        key, _, val = key_val.strip().partition("=")
        config[key.strip()] = {"value": val.strip(), "reason": reason.strip()}
        print(f"  {key.strip():<25} = {val.strip():<15}  # {reason.strip()}")

print(f"\nTotal config keys: {len(config)}")
```

**Expected Token Savings:** 55–70% versus XML for configuration outputs; key=value is zero-overhead and natively parseable by Python's `configparser` and most `.env` libraries.

**Environment:** DevOps agents, infrastructure configurators, settings recommendation systems.

---

### Option 5 — Markdown table for human-readable structured output

```python
import anthropic
import re

client = anthropic.Anthropic()

# XML for comparison table
XML_SYSTEM = """
Compare three options and respond in XML:
<comparison>
  <option>
    <name>string</name>
    <pros>string</pros>
    <cons>string</cons>
    <cost_per_month_usd>integer</cost_per_month_usd>
    <recommended_for>string</recommended_for>
  </option>
</comparison>
"""

# Markdown table (human-readable, compact)
TABLE_SYSTEM = """
Compare the options in a markdown table. No prose before or after.
Columns: Option | Pros | Cons | Cost/mo | Best For
Keep each cell under 8 words.
"""

QUERY = "Compare AWS RDS, PlanetScale, and Supabase for a SaaS startup."

def call(system: str) -> tuple[str, int]:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=384,
        system=system,
        messages=[{"role": "user", "content": QUERY}],
    )
    return resp.content[0].text.strip(), resp.usage.output_tokens

xml_out,   xml_tok   = call(XML_SYSTEM)
table_out, table_tok = call(TABLE_SYSTEM)

print(f"XML   tokens: {xml_tok}")
print(f"Table tokens: {table_tok}")
print(f"Savings: {xml_tok - table_tok} tokens ({(1 - table_tok/xml_tok)*100:.0f}%)")
print(f"\nMarkdown table:\n{table_out}")

# Parse markdown table into dicts
def parse_md_table(md: str) -> list[dict]:
    lines = [l.strip() for l in md.splitlines() if l.strip().startswith("|")]
    if len(lines) < 3:
        return []
    headers = [h.strip() for h in lines[0].strip("|").split("|")]
    rows = []
    for line in lines[2:]:  # skip header separator
        cells = [c.strip() for c in line.strip("|").split("|")]
        rows.append(dict(zip(headers, cells)))
    return rows

rows = parse_md_table(table_out)
print(f"\nParsed {len(rows)} rows from table")
```

**Expected Token Savings:** 40–60% versus XML; markdown tables are human-readable without rendering and directly embeddable in documentation, Slack messages, and GitHub issues.

**Environment:** Comparison agents, recommendation systems, any output that humans will read directly.

---

### Option 6 — Format router: auto-select compact format based on output type

```python
import anthropic
import json
import csv
import io

client = anthropic.Anthropic()

FORMAT_ROUTER_SYSTEM = """
You are a data formatter. Based on the request type, respond in the most compact format:

- If the output is a TABLE (multiple rows of uniform data): use CSV with a header row.
- If the output is a SINGLE OBJECT (one record): use minified JSON, no spaces.
- If the output is a LIST of SHORT ITEMS: use newline-separated plain text, one item per line.
- If the output is CONFIG (key=value pairs): use KEY=VALUE format.

DO NOT use XML. DO NOT use indented JSON. DO NOT add prose before or after the data.
""".strip()

def detect_format(text: str) -> str:
    text = text.strip()
    if text.startswith("{") or text.startswith("["):
        return "json"
    if "," in text.splitlines()[0] if text.splitlines() else "":
        return "csv"
    if "=" in text.splitlines()[0] if text.splitlines() else "":
        return "kv"
    return "text"

def call_and_parse(user_message: str) -> tuple[str, any, int]:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=FORMAT_ROUTER_SYSTEM,
        messages=[{"role": "user", "content": user_message}],
    )
    raw = resp.content[0].text.strip()
    fmt = detect_format(raw)

    parsed: any = raw
    try:
        if fmt == "json":
            parsed = json.loads(raw)
        elif fmt == "csv":
            parsed = list(csv.DictReader(io.StringIO(raw)))
        elif fmt == "kv":
            parsed = dict(
                line.split("=", 1)
                for line in raw.splitlines()
                if "=" in line and not line.startswith("#")
            )
    except Exception as e:
        print(f"[PARSE WARN] {e}")

    return fmt, parsed, resp.usage.output_tokens

queries = [
    "List the top 5 Python web frameworks by popularity.",
    "Give me a single summary object for Flask: creator, year, license, stars.",
    "Recommend uvicorn config settings for production.",
    "Compare FastAPI, Django, Flask on speed, features, learning curve.",
]

for query in queries:
    fmt, parsed, tokens = call_and_parse(query)
    print(f"\n[{tokens} tokens | {fmt.upper()}] {query[:50]}")
    if fmt == "json":
        print(json.dumps(parsed, indent=2)[:200])
    elif fmt == "csv" and isinstance(parsed, list) and parsed:
        headers = list(parsed[0].keys())
        print(f"  Columns: {headers}")
        print(f"  Rows: {len(parsed)}")
    elif fmt == "kv":
        for k, v in list(parsed.items())[:4]:
            print(f"  {k}={v}")
    else:
        lines = str(parsed).splitlines()
        for line in lines[:5]:
            print(f"  {line}")
```

**Expected Token Savings:** 45–70% across all output types compared to XML; the router eliminates format negotiation overhead and always picks the minimum-token representation for the data shape.

**Environment:** General-purpose agents handling diverse output types; the router system prompt adds ~80 tokens once but saves hundreds across every response.

---

## Comparison

| Option | Format | Token Overhead | Parseable | Human-Readable | Best For |
|--------|--------|---------------|---------|---------------|---------|
| 1 — Minified JSON | `{}` | Minimal | Always | Moderate | Machine consumption |
| 2 — Short-tag XML | `<r><p>h</p></r>` | Low | XML parsers | Poor | XML-required pipelines |
| 3 — CSV | `a,b,c` | None | csv module | Good | Tabular data |
| 4 — Key=value | `K=V` | None | configparser | Good | Config outputs |
| 5 — Markdown table | `\| a \| b \|` | Low | Custom parser | Excellent | Human-facing comparisons |
| 6 — Format router | Any | Router ~80 tok | Type-aware | Varies | Mixed output types |

**Recommended default:** Option 1 (minified JSON) for API-consumed outputs; Option 3 (CSV) for tabular data; Option 6 (format router) when a single agent handles multiple output shapes.
