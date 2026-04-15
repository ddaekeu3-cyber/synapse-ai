---
layout: solution
title: "Agent hallucinates database schema"
category: hallucination
description: "Agent invents table names, column names, or relationships that don't exist in the actual database schema, producing SQL that fails at runtime."
tags: [hallucination, database, schema, sql, tool-failure]
---

## Symptom

The agent generates SQL queries that reference non-existent tables or columns. The database rejects them with errors like `relation "user_profiles" does not exist` or `column "created_at" does not exist`. The agent was trained on many database schemas and blends them together, producing plausible-sounding but wrong queries.

```sql
-- Agent generates (hallucinated):
SELECT u.email, p.bio FROM users u JOIN user_profiles p ON u.id = p.user_id;

-- Actual schema has:
-- Table: accounts (id, email, username)
-- Table: profiles (account_id, description)  ← different names
```

## Root Cause

The model has no access to the actual schema at inference time. It generates SQL from statistical patterns in training data rather than from ground truth. Common hallucination patterns: adding `_at` suffix to timestamp columns, assuming junction table names, and guessing foreign key column names by convention.

## Fix

Inject the actual schema into the system prompt or tool context before any SQL is generated, and validate generated queries against the schema before execution.

---

### Option 1 — Schema injection in system prompt before SQL generation

```python
import anthropic
import json
import sqlite3
import re

client = anthropic.Anthropic()

# Simulate actual database schema
SCHEMA = {
    "accounts": {
        "columns": ["id INTEGER PRIMARY KEY", "email TEXT NOT NULL", "username TEXT", "joined_epoch INTEGER"],
        "foreign_keys": [],
    },
    "profiles": {
        "columns": ["account_id INTEGER PRIMARY KEY", "description TEXT", "avatar_url TEXT"],
        "foreign_keys": ["account_id REFERENCES accounts(id)"],
    },
    "orders": {
        "columns": ["id INTEGER PRIMARY KEY", "account_id INTEGER", "total_cents INTEGER", "placed_epoch INTEGER"],
        "foreign_keys": ["account_id REFERENCES accounts(id)"],
    },
}

def schema_to_prompt(schema: dict) -> str:
    lines = ["## Exact Database Schema (use ONLY these tables and columns)\n"]
    for table, info in schema.items():
        lines.append(f"### {table}")
        lines.append("Columns: " + ", ".join(info["columns"]))
        if info["foreign_keys"]:
            lines.append("Foreign keys: " + ", ".join(info["foreign_keys"]))
        lines.append("")
    lines.append("Do NOT use any table or column name not listed above.")
    return "\n".join(lines)

SCHEMA_PROMPT = schema_to_prompt(SCHEMA)

def generate_sql(user_question: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=(
            f"{SCHEMA_PROMPT}\n\n"
            "Generate a single valid SQLite query. "
            "Return ONLY the SQL statement — no explanation, no markdown."
        ),
        messages=[{"role": "user", "content": user_question}],
    )
    return response.content[0].text.strip()

def validate_sql_columns(sql: str, schema: dict) -> list[str]:
    """Check that every identifier in the SQL exists in the schema."""
    errors = []
    known_tables  = set(schema.keys())
    known_columns = {col.split()[0] for table in schema.values() for col in table["columns"]}

    # Naive check: extract word tokens that look like identifiers
    tokens = set(re.findall(r"\b([a-z_][a-z0-9_]*)\b", sql.lower()))
    sql_keywords = {"select","from","where","join","on","and","or","not","in","is","null",
                    "as","by","order","group","having","limit","inner","left","right","outer",
                    "integer","text","primary","key","references","count","sum","max","min","avg"}
    identifiers = tokens - sql_keywords

    for ident in identifiers:
        if ident not in known_tables and ident not in known_columns:
            errors.append(f"Unknown identifier: '{ident}'")

    return errors

# Test
questions = [
    "Show me all users who placed an order in the last 30 days.",
    "Get the email and description for account id 5.",
    "Count orders per account ordered by total.",
]

for q in questions:
    sql = generate_sql(q)
    errors = validate_sql_columns(sql, SCHEMA)
    status = "✓" if not errors else f"✗ {errors}"
    print(f"Q: {q[:55]}")
    print(f"SQL: {sql[:100]}")
    print(f"Validation: {status}\n")
```

**Expected Token Savings:** Prevents multiple failed SQL attempts; schema injection adds ~200 tokens once but saves 2–5 retry cycles of 500+ tokens each.

**Environment:** Any SQL-generating agent; keep the schema block in a prompt-cached system prompt for zero per-call overhead after the first request.

---

### Option 2 — Introspect live schema and inject at query time

```python
import anthropic
import sqlite3

client = anthropic.Anthropic()

def get_live_schema(conn: sqlite3.Connection) -> str:
    """Read actual schema from the live database — zero hallucination risk."""
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;")
    tables = [row[0] for row in cursor.fetchall()]

    lines = ["## Live Database Schema\n"]
    for table in tables:
        cursor.execute(f"PRAGMA table_info({table});")
        cols = cursor.fetchall()
        cursor.execute(f"PRAGMA foreign_key_list({table});")
        fks = cursor.fetchall()

        col_defs = [f"{c[1]} {c[2]}{'(PK)' if c[5] else ''}" for c in cols]
        fk_defs  = [f"{fk[3]} → {fk[2]}({fk[4]})" for fk in fks]

        lines.append(f"### {table}")
        lines.append("Columns: " + ", ".join(col_defs))
        if fk_defs:
            lines.append("FKs: " + ", ".join(fk_defs))
        lines.append("")

    return "\n".join(lines)

def setup_demo_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        PRAGMA foreign_keys = ON;
        CREATE TABLE accounts (
            id INTEGER PRIMARY KEY,
            email TEXT NOT NULL,
            username TEXT,
            joined_epoch INTEGER
        );
        CREATE TABLE profiles (
            account_id INTEGER PRIMARY KEY REFERENCES accounts(id),
            description TEXT,
            avatar_url TEXT
        );
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            account_id INTEGER REFERENCES accounts(id),
            total_cents INTEGER,
            placed_epoch INTEGER
        );
        INSERT INTO accounts VALUES (1,'alice@x.com','alice',1700000000);
        INSERT INTO accounts VALUES (2,'bob@x.com','bob',1710000000);
        INSERT INTO profiles VALUES (1,'Python dev','https://cdn/alice.jpg');
        INSERT INTO orders VALUES (1,1,4999,1714000000);
        INSERT INTO orders VALUES (2,1,9900,1715000000);
        INSERT INTO orders VALUES (3,2,1499,1716000000);
    """)
    return conn

def ask_db(question: str, conn: sqlite3.Connection) -> str:
    schema = get_live_schema(conn)

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=(
            f"{schema}\n"
            "Write a single valid SQLite query for the question. "
            "Return ONLY the SQL — no markdown, no explanation."
        ),
        messages=[{"role": "user", "content": question}],
    )

    sql = response.content[0].text.strip().rstrip(";") + ";"
    print(f"Generated SQL: {sql}")

    try:
        cursor = conn.cursor()
        cursor.execute(sql)
        rows = cursor.fetchall()
        cols = [d[0] for d in cursor.description] if cursor.description else []
        return f"Columns: {cols}\nRows: {rows}"
    except sqlite3.Error as e:
        return f"SQL ERROR: {e}"

conn = setup_demo_db()

questions = [
    "How many orders has each account placed? Show username and count.",
    "Get the email and profile description for all accounts that have a profile.",
    "What is the total spending in cents for account 1?",
]

for q in questions:
    print(f"\nQ: {q}")
    result = ask_db(q, conn)
    print(f"Result: {result}")
```

**Expected Token Savings:** Eliminates all schema hallucinations; live introspection is authoritative and costs 0 tokens since it runs locally before the API call.

**Environment:** SQLite, PostgreSQL, MySQL agents; replace `PRAGMA` calls with `information_schema` queries for PostgreSQL/MySQL.

---

### Option 3 — Schema tool: agent must call schema lookup before writing SQL

```python
import anthropic
import json

client = anthropic.Anthropic()

# Ground-truth schema (in production: query information_schema)
SCHEMA_DB = {
    "accounts":  ["id", "email", "username", "joined_epoch"],
    "profiles":  ["account_id", "description", "avatar_url"],
    "orders":    ["id", "account_id", "total_cents", "placed_epoch"],
    "line_items":["id", "order_id", "product_id", "quantity", "price_cents"],
    "products":  ["id", "name", "description", "price_cents", "stock_count"],
}

def get_schema(table_name: str) -> str:
    if table_name == "all":
        return json.dumps({t: cols for t, cols in SCHEMA_DB.items()})
    if table_name in SCHEMA_DB:
        return json.dumps({"table": table_name, "columns": SCHEMA_DB[table_name]})
    return json.dumps({"error": f"Table '{table_name}' does not exist. Known tables: {list(SCHEMA_DB.keys())}"})

TOOLS = [
    {
        "name": "get_schema",
        "description": "Look up the database schema for a table before writing SQL. Use 'all' to list all tables.",
        "input_schema": {
            "type": "object",
            "properties": {
                "table_name": {
                    "type": "string",
                    "description": "Table name to inspect, or 'all' to list all tables.",
                }
            },
            "required": ["table_name"],
        },
    },
    {
        "name": "execute_sql",
        "description": "Execute a SQL query against the database.",
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string", "description": "The SQL query to execute."}},
            "required": ["sql"],
        },
    },
]

SYSTEM = (
    "You are a SQL assistant. IMPORTANT: Before writing any SQL, you MUST call get_schema "
    "to verify the exact table and column names. Never guess column names."
)

def run_sql_agent(question: str) -> str:
    messages = [{"role": "user", "content": question}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            break

        results = []
        for b in response.content:
            if b.type != "tool_use":
                continue
            if b.name == "get_schema":
                content = get_schema(b.input.get("table_name", "all"))
                print(f"  [SCHEMA LOOKUP] {b.input.get('table_name')}: {content[:80]}")
            elif b.name == "execute_sql":
                sql = b.input.get("sql", "")
                content = json.dumps({"simulated_result": "OK", "sql": sql})
                print(f"  [EXECUTE SQL] {sql[:80]}")
            else:
                content = json.dumps({"error": "unknown tool"})

            results.append({"type": "tool_result", "tool_use_id": b.id, "content": content})

        messages.append({"role": "user", "content": results})

    return next(b.text for b in response.content if hasattr(b, "text"))

print(run_sql_agent("Show me all orders with their line items and product names."))
```

**Expected Token Savings:** Forces schema verification before SQL generation; the model cannot hallucinate columns it just verified don't exist; ~100 token overhead per schema lookup eliminates multi-turn error-recovery cycles.

**Environment:** Agentic SQL systems where the model has broad discretion; the mandatory schema-first discipline is enforced by the tool availability, not by prompt instruction alone.

---

### Option 4 — SQL validator that re-prompts on schema error

```python
import anthropic
import re
import json

client = anthropic.Anthropic()

KNOWN_SCHEMA = {
    "accounts":   {"id", "email", "username", "joined_epoch"},
    "profiles":   {"account_id", "description", "avatar_url"},
    "orders":     {"id", "account_id", "total_cents", "placed_epoch"},
}

SQL_KEYWORDS = {
    "select","from","where","join","inner","left","right","outer","on","and","or",
    "not","in","is","null","as","by","order","group","having","limit","count",
    "sum","max","min","avg","distinct","case","when","then","else","end",
    "integer","text","asc","desc","insert","update","delete","create","drop",
}

def validate_schema(sql: str) -> list[str]:
    tokens = set(re.findall(r"\b([a-z_][a-z0-9_]*)\b", sql.lower()))
    identifiers = tokens - SQL_KEYWORDS

    all_tables  = set(KNOWN_SCHEMA.keys())
    all_columns = {col for cols in KNOWN_SCHEMA.values() for col in cols}
    known = all_tables | all_columns

    return [t for t in identifiers if t not in known and len(t) > 2]

SCHEMA_DESCRIPTION = json.dumps({t: list(cols) for t, cols in KNOWN_SCHEMA.items()}, indent=2)

def generate_validated_sql(question: str, max_attempts: int = 3) -> str:
    messages = [{"role": "user", "content": question}]
    system = (
        f"Generate a SQLite query. Use ONLY these tables and columns:\n{SCHEMA_DESCRIPTION}\n"
        "Return ONLY the SQL — no markdown, no explanation."
    )

    for attempt in range(max_attempts):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=system,
            messages=messages,
        )
        sql = response.content[0].text.strip()
        print(f"[Attempt {attempt+1}] {sql[:80]}")

        unknown = validate_schema(sql)
        if not unknown:
            print(f"[VALID] ✓")
            return sql

        error_msg = (
            f"Your SQL uses unknown identifiers: {unknown}. "
            f"Only use these exact names:\n{SCHEMA_DESCRIPTION}\n"
            "Rewrite the query using only valid identifiers."
        )
        print(f"[INVALID] unknown={unknown}")
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": error_msg})

    return sql  # return best attempt after max retries

for q in [
    "Get user emails with their profile bios.",
    "Show total order value per user.",
]:
    print(f"\nQ: {q}")
    result = generate_validated_sql(q)
    print(f"Final SQL: {result}")
```

**Expected Token Savings:** Catches hallucinations before database execution; re-prompt with specific error is cheaper than handling a DB exception and restarting from scratch.

**Environment:** Any SQL-generating agent; the validator is a pure Python function with zero API cost.

---

### Option 5 — Few-shot examples anchored to actual schema

```python
import anthropic

client = anthropic.Anthropic()

SCHEMA_AND_EXAMPLES = """
## Database Schema

Table: accounts
  Columns: id, email, username, joined_epoch

Table: profiles
  Columns: account_id, description, avatar_url

Table: orders
  Columns: id, account_id, total_cents, placed_epoch

## Correct SQL Examples (use these as templates)

Q: List all accounts.
A: SELECT id, email, username FROM accounts;

Q: Get profile for account 1.
A: SELECT description, avatar_url FROM profiles WHERE account_id = 1;

Q: Count orders per account.
A: SELECT account_id, COUNT(*) AS order_count FROM orders GROUP BY account_id;

Q: Get email and total spending per account.
A: SELECT a.email, SUM(o.total_cents) AS total
   FROM accounts a
   JOIN orders o ON a.id = o.account_id
   GROUP BY a.id, a.email;

Only use columns and tables defined above. Never invent new column names.
""".strip()

def sql_from_example(question: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=SCHEMA_AND_EXAMPLES + "\n\nReturn ONLY the SQL query.",
        messages=[{"role": "user", "content": f"Q: {question}\nA:"}],
    )
    return response.content[0].text.strip()

queries = [
    "Show all accounts that have a profile.",
    "Get the 5 largest orders by total_cents.",
    "Find accounts with no orders.",
]

for q in queries:
    sql = sql_from_example(q)
    print(f"Q: {q}")
    print(f"SQL: {sql}\n")
```

**Expected Token Savings:** Few-shot examples provide strong schema anchoring; the model learns field names from examples rather than training memory; adds ~300 tokens once, saved in cache on subsequent calls.

**Environment:** Agents with a stable schema and a fixed set of common query patterns; most effective when examples cover the column-naming conventions used in the actual database.

---

### Option 6 — Schema-diff alert: detect when generated SQL diverges from schema version

```python
import anthropic
import json
import hashlib
import re

client = anthropic.Anthropic()

# Schema versioning — detect when cached schema is stale
CURRENT_SCHEMA = {
    "version": "2025-04-15",
    "tables": {
        "accounts":   ["id", "email", "username", "joined_epoch"],
        "profiles":   ["account_id", "description", "avatar_url"],
        "orders":     ["id", "account_id", "total_cents", "placed_epoch"],
    }
}

def schema_hash(schema: dict) -> str:
    return hashlib.sha256(json.dumps(schema["tables"], sort_keys=True).encode()).hexdigest()[:8]

def build_schema_system(schema: dict) -> str:
    h = schema_hash(schema)
    lines = [f"## Database Schema v{schema['version']} (hash: {h})\n"]
    for table, cols in schema["tables"].items():
        lines.append(f"{table}: {', '.join(cols)}")
    lines.append(
        "\nGenerate SQL using ONLY these exact table and column names. "
        f"This schema has hash {h} — if your SQL uses identifiers not listed above, "
        "it will be rejected."
    )
    return "\n".join(lines)

def extract_identifiers_from_sql(sql: str) -> set[str]:
    tokens = set(re.findall(r"\b([a-z_][a-z0-9_]*)\b", sql.lower()))
    sql_keywords = {
        "select","from","where","join","on","and","or","not","in","is","null",
        "as","by","order","group","having","limit","inner","left","outer","right",
        "count","sum","max","min","avg","distinct","case","when","then","else","end",
    }
    return tokens - sql_keywords

def schema_drift_check(sql: str, schema: dict) -> dict:
    known = {col for cols in schema["tables"].values() for col in cols} | set(schema["tables"].keys())
    identifiers = extract_identifiers_from_sql(sql)
    unknown = {i for i in identifiers if i not in known and len(i) > 2}
    return {
        "valid": len(unknown) == 0,
        "unknown_identifiers": list(unknown),
        "schema_hash": schema_hash(schema),
        "schema_version": schema["version"],
    }

system_prompt = build_schema_system(CURRENT_SCHEMA)

def run(question: str) -> dict:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system_prompt,
        messages=[{"role": "user", "content": f"Write SQL: {question}"}],
    )
    sql = resp.content[0].text.strip()
    check = schema_drift_check(sql, CURRENT_SCHEMA)
    return {"sql": sql, "check": check}

test_questions = [
    "List accounts with their most recent order date.",
    "Show profile descriptions for accounts created after epoch 1700000000.",
]

for q in test_questions:
    result = run(q)
    print(f"Q: {q}")
    print(f"SQL: {result['sql']}")
    print(f"Check: {result['check']}\n")
```

**Expected Token Savings:** Schema version hash enables cache invalidation — when the schema changes, the system prompt changes and the old cached prompt is not reused, preventing stale-schema hallucinations; schema drift alerts surface quickly before they reach production.

**Environment:** Production agents where the schema evolves; version-stamp the schema in your CI pipeline and regenerate the system prompt on schema migrations.

---

## Comparison

| Option | Schema Source | Hallucination Prevention | Runtime Cost | Best For |
|--------|-------------|------------------------|-------------|---------|
| 1 — Injection in prompt | Static dict | High | ~200 tokens/call | Stable schemas |
| 2 — Live introspection | Database | Perfect | 0 tokens (local) | Any live DB |
| 3 — Schema tool | Tool call | Forced verification | ~100 tokens/lookup | Agentic SQL |
| 4 — SQL validator + re-prompt | Static dict | Catch-and-correct | 0 tokens (Python) | Any agent |
| 5 — Few-shot examples | Static examples | High (pattern matching) | ~300 tokens once | Common query patterns |
| 6 — Schema-diff alert | Versioned dict | High + drift detection | 0 tokens (Python) | Evolving schemas |

**Recommended default:** Option 2 (live introspection) for any agent with database access — the schema comes directly from the source of truth and costs nothing extra. Combine with Option 4 (SQL validator) for defense-in-depth.
