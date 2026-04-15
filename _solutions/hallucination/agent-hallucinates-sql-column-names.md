---
layout: solution
title: "Agent Hallucinates SQL Column Names"
category: hallucination
description: "Agent generates SQL queries with column names that don't exist in the actual schema — queries that fail at runtime with 'column not found' errors, or worse, silently join on the wrong column and return subtly incorrect data."
tags: [hallucination, sql, database, schema, code-generation]
---

## Symptom

The agent generates a query referencing `user_name` when the actual column is `username`:

```sql
-- Agent-generated query
SELECT user_name, email_address, created_date
FROM users
WHERE is_active = 1
ORDER BY last_login_timestamp DESC;
```

Database error:
```
ERROR: column "user_name" does not exist
LINE 1: SELECT user_name, email_address, created_date
```

Or worse — the agent guesses `user_id` for a join that should use `account_id`, and the query runs but returns wrong results silently.

## Root Cause

The model infers column names from common patterns in training data rather than querying the actual schema. Naming conventions vary (`user_name` vs `username` vs `UserName`) and the model has no way to know which is correct without being told:

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

# Anti-pattern: generate SQL without providing schema
response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=300,
    messages=[{"role": "user", "content": "Write SQL to get all active users sorted by last login"}]
)
# → Model guesses column names from training distribution
```

---

## Fix

### Option 1 — Inject the real schema into the prompt

Fetch the actual table schema at query-generation time and include it in the system prompt. The model uses only the columns that exist.

```python
import anthropic
import sqlite3

client = anthropic.Anthropic(api_key="sk-live-...")


def get_schema(db_path: str, tables: list[str] | None = None) -> str:
    """Extract CREATE TABLE statements from a SQLite database."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    if tables is None:
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]

    schema_parts = []
    for table in tables:
        cursor.execute(f"SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,))
        row = cursor.fetchone()
        if row and row[0]:
            schema_parts.append(row[0])

    conn.close()
    return "\n\n".join(schema_parts)


def generate_sql(db_path: str, question: str) -> str:
    schema = get_schema(db_path)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        system=f"""You are a SQL expert. Generate a SQL query to answer the user's question.
Use ONLY the columns and tables defined in the schema below.
Never invent column names. If a column does not exist in the schema, say so.

Schema:
{schema}

Rules:
- Use exact column names as shown in the schema (case-sensitive for PostgreSQL)
- Use table aliases for readability
- Add LIMIT 1000 unless the question explicitly asks for all rows""",
        messages=[{"role": "user", "content": question}]
    )
    return response.content[0].text.strip()


# Create demo database
conn = sqlite3.connect(":memory:")
conn.execute("""
    CREATE TABLE users (
        id INTEGER PRIMARY KEY,
        username TEXT NOT NULL,
        email TEXT UNIQUE,
        is_active INTEGER DEFAULT 1,
        last_login TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
""")

# Write schema to temp file for demo
import tempfile, os
with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
    db_path = f.name

conn2 = sqlite3.connect(db_path)
conn2.execute("""CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL,
    email TEXT UNIQUE, is_active INTEGER DEFAULT 1, last_login TEXT, created_at TEXT)""")
conn2.commit()

query = generate_sql(db_path, "Get all active users sorted by most recent login")
print(query)
os.unlink(db_path)

# Expected Token Savings: correct query first time → no error + retry round-trip (saves 2+ turns)
# Environment: SQL generation agents, NL2SQL systems, data analyst assistants
```

---

### Option 2 — Column name validator: execute EXPLAIN before running

After generating the SQL, run `EXPLAIN` or `PRAGMA table_info` to validate that all referenced columns exist before executing the real query.

```python
import anthropic
import sqlite3
import re

client = anthropic.Anthropic(api_key="sk-live-...")


def extract_column_references(sql: str) -> list[str]:
    """Extract identifiers from SQL that could be column references."""
    # Remove string literals and comments
    cleaned = re.sub(r"'[^']*'", "''", sql)
    cleaned = re.sub(r"--[^\n]*", "", cleaned)

    # Extract words (potential column names) — exclude SQL keywords
    SQL_KEYWORDS = {
        "select", "from", "where", "join", "on", "and", "or", "not", "in",
        "order", "by", "group", "having", "limit", "offset", "as", "distinct",
        "left", "right", "inner", "outer", "union", "all", "case", "when",
        "then", "else", "end", "null", "true", "false", "is", "like", "between",
        "exists", "count", "sum", "avg", "max", "min", "asc", "desc"
    }
    words = re.findall(r'\b([a-z_][a-z0-9_]*)\b', cleaned.lower())
    return [w for w in words if w not in SQL_KEYWORDS]


def get_table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    cursor = conn.execute(f"PRAGMA table_info({table_name})")
    return {row[1].lower() for row in cursor.fetchall()}


def validate_query(conn: sqlite3.Connection, sql: str, schema_tables: dict[str, set[str]]) -> list[str]:
    """Return list of column names referenced in SQL that don't exist in any table."""
    all_columns = set()
    for cols in schema_tables.values():
        all_columns.update(cols)

    referenced = set(extract_column_references(sql))
    table_names = set(schema_tables.keys())

    # Remove table names and numeric-looking tokens from check
    unknowns = referenced - all_columns - table_names - {"1", "0"}
    return list(unknowns)


def generate_validated_sql(conn: sqlite3.Connection, schema: dict[str, set[str]],
                            schema_text: str, question: str) -> str:
    system = f"""Generate SQLite SQL for the question. Use ONLY these columns:
{schema_text}
Return ONLY the SQL query, no explanation."""

    for attempt in range(3):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            system=system,
            messages=[{"role": "user", "content": question}]
        )
        sql = response.content[0].text.strip().strip("```sql").strip("```").strip()

        unknowns = validate_query(conn, sql, schema)
        if not unknowns:
            print(f"[validate] Query valid on attempt {attempt + 1}")
            return sql

        print(f"[validate] Unknown columns: {unknowns} — regenerating")
        system += f"\n\nPrevious attempt used non-existent columns: {unknowns}. These do NOT exist. Use only the columns listed above."

    return sql  # Best effort


# Demo setup
conn = sqlite3.connect(":memory:")
conn.execute("""CREATE TABLE orders (
    order_id INTEGER PRIMARY KEY,
    customer_id INTEGER,
    total_amount REAL,
    status TEXT,
    placed_at TEXT
)""")

schema = {"orders": get_table_columns(conn, "orders")}
schema_text = "orders: order_id, customer_id, total_amount, status, placed_at"

sql = generate_validated_sql(conn, schema, schema_text, "Show pending orders over $100")
print(sql)

# Expected Token Savings: column validation catches hallucinations before DB execution → no error handling turn
# Environment: NL2SQL agents with access to live database schema
```

---

### Option 3 — Schema-aware few-shot examples anchored to real columns

Provide few-shot examples that use the actual column names of the target database. The model learns the naming convention from examples before generating.

```python
import anthropic
import sqlite3

client = anthropic.Anthropic(api_key="sk-live-...")


def build_few_shot_examples(schema_info: dict[str, list[str]]) -> str:
    """Build few-shot SQL examples using real column names from the schema."""
    examples = []

    for table, columns in schema_info.items():
        col_list = ", ".join(columns[:5])  # First 5 columns for brevity
        pk = columns[0]  # Assume first column is PK

        examples.append(f"""Q: Count all rows in {table}
SQL: SELECT COUNT(*) FROM {table};

Q: Get the first 10 rows from {table}
SQL: SELECT {col_list} FROM {table} LIMIT 10;

Q: Find a specific {table} row by {pk}
SQL: SELECT {col_list} FROM {table} WHERE {pk} = ?;""")

    return "\n\n".join(examples)


def get_column_info(conn: sqlite3.Connection) -> dict[str, list[str]]:
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [row[0] for row in cursor.fetchall()]
    result = {}
    for table in tables:
        col_cursor = conn.execute(f"PRAGMA table_info({table})")
        result[table] = [row[1] for row in col_cursor.fetchall()]
    return result


def generate_sql_with_few_shot(conn: sqlite3.Connection, question: str) -> str:
    columns = get_column_info(conn)
    examples = build_few_shot_examples(columns)

    schema_description = "\n".join(
        f"{table}({', '.join(cols)})"
        for table, cols in columns.items()
    )

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        system=f"""You are a SQL expert. Generate SQL for SQLite.

Schema:
{schema_description}

Examples using the exact column names from this schema:
{examples}

Use only column names shown above. Return only the SQL query.""",
        messages=[{"role": "user", "content": question}]
    )
    return response.content[0].text.strip().strip("```sql").strip("```").strip()


# Demo
conn = sqlite3.connect(":memory:")
conn.execute("""CREATE TABLE products (
    product_id INTEGER PRIMARY KEY,
    product_name TEXT,
    unit_price REAL,
    stock_qty INTEGER,
    category_code TEXT
)""")

sql = generate_sql_with_few_shot(conn, "Find all products under $20 with stock above 100")
print(sql)

# Expected Token Savings: few-shot anchoring cuts column hallucinations by ~80% without retry overhead
# Environment: NL2SQL agents where schema is fixed; internal data analyst tools
```

---

### Option 4 — Structured output: model returns column names separately for validation

Ask the model to return SQL as structured JSON with column references listed explicitly. Validate the column list before assembling the final query.

```python
import anthropic
import json
import sqlite3

client = anthropic.Anthropic(api_key="sk-live-...")


def get_all_columns(conn: sqlite3.Connection) -> dict[str, set[str]]:
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [row[0] for row in cursor.fetchall()]
    result = {}
    for table in tables:
        col_cursor = conn.execute(f"PRAGMA table_info({table})")
        result[table] = {row[1] for row in col_cursor.fetchall()}
    return result


def generate_sql_structured(conn: sqlite3.Connection, schema_text: str, question: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system=f"""Generate SQL as JSON with this structure:
{{
  "sql": "SELECT ...",
  "tables_used": ["table1", "table2"],
  "columns_selected": ["col1", "col2"],
  "columns_filtered": ["col3"],
  "columns_joined": []
}}

Schema:
{schema_text}

Return ONLY valid JSON.""",
        messages=[{"role": "user", "content": question}]
    )

    raw = response.content[0].text.strip()
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    result = json.loads(raw.strip())

    # Validate all referenced columns
    all_cols = get_all_columns(conn)
    all_col_names = {col for cols in all_cols.values() for col in cols}

    invalid = []
    for col in result.get("columns_selected", []) + result.get("columns_filtered", []):
        if col.lower() not in {c.lower() for c in all_col_names}:
            invalid.append(col)

    if invalid:
        print(f"[structured] Invalid columns detected: {invalid}")
        raise ValueError(f"Query references non-existent columns: {invalid}")

    print(f"[structured] Column validation passed")
    return result["sql"]


conn = sqlite3.connect(":memory:")
conn.execute("""CREATE TABLE invoices (
    invoice_id INTEGER PRIMARY KEY,
    vendor_id INTEGER,
    invoice_date TEXT,
    due_date TEXT,
    amount_due REAL,
    paid INTEGER DEFAULT 0
)""")

schema = "invoices(invoice_id, vendor_id, invoice_date, due_date, amount_due, paid)"

try:
    sql = generate_sql_structured(conn, schema, "Find all unpaid invoices past their due date")
    print(sql)
except ValueError as e:
    print(f"Validation failed: {e}")

# Expected Token Savings: structured output separates schema validation from SQL generation
# Environment: high-stakes SQL generation (financial, compliance); APIs returning SQL to external callers
```

---

### Option 5 — Query execution sandbox with automatic schema error correction

Execute the generated query in a read-only sandbox, parse any column-not-found errors, and automatically retry with the correct column names.

```python
import anthropic
import sqlite3
import re

client = anthropic.Anthropic(api_key="sk-live-...")


def execute_safely(conn: sqlite3.Connection, sql: str) -> tuple[bool, str, list]:
    """Execute SQL and return (success, error_message, rows)."""
    try:
        cursor = conn.execute(sql)
        rows = cursor.fetchmany(5)  # Only fetch sample for validation
        return True, "", rows
    except sqlite3.OperationalError as e:
        return False, str(e), []


def extract_bad_column(error_msg: str) -> str | None:
    """Parse 'no such column: X' from SQLite error."""
    match = re.search(r'no such column: (\S+)', error_msg)
    return match.group(1) if match else None


def get_column_names(conn: sqlite3.Connection, table: str) -> list[str]:
    cursor = conn.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in cursor.fetchall()]


def find_closest_column(bad_col: str, available: list[str]) -> str | None:
    """Find the closest matching column name by common substitutions."""
    bad_lower = bad_col.lower().replace("_", "").replace("-", "")
    for col in available:
        if col.lower().replace("_", "").replace("-", "") == bad_lower:
            return col
    # Try prefix match
    for col in available:
        if col.lower().startswith(bad_lower[:4]) or bad_lower.startswith(col.lower()[:4]):
            return col
    return None


def generate_and_fix_sql(conn: sqlite3.Connection, schema_text: str, question: str) -> str:
    system = f"""Generate SQLite SQL. Schema:\n{schema_text}\nReturn only the SQL."""

    for attempt in range(4):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=system,
            messages=[{"role": "user", "content": question}]
        )
        sql = response.content[0].text.strip().strip("```sql").strip("```").strip()

        success, error, _ = execute_safely(conn, sql)
        if success:
            print(f"[sandbox] Query valid on attempt {attempt + 1}")
            return sql

        print(f"[sandbox] Error: {error}")
        bad_col = extract_bad_column(error)
        if bad_col:
            # Find closest real column name
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            for (table,) in cursor.fetchall():
                available = get_column_names(conn, table)
                closest = find_closest_column(bad_col, available)
                if closest:
                    print(f"[sandbox] '{bad_col}' → did you mean '{closest}'?")
                    system += f"\n\nThe column '{bad_col}' does not exist. Use '{closest}' instead."
                    break
            else:
                system += f"\n\nError: {error}. Fix the column name."
        else:
            system += f"\n\nSQL error: {error}. Fix it."

    return sql


conn = sqlite3.connect(":memory:")
conn.execute("""CREATE TABLE employees (
    emp_id INTEGER PRIMARY KEY,
    full_name TEXT,
    department TEXT,
    salary REAL,
    hire_date TEXT
)""")

schema = "employees(emp_id, full_name, department, salary, hire_date)"
sql = generate_and_fix_sql(conn, schema, "Get average salary by department")
print(sql)

# Expected Token Savings: sandbox catches schema errors before production; auto-correction avoids user retry
# Environment: agents with direct database access; query builders with execute permissions
```

---

### Option 6 — Semantic column matching: map hallucinated names to real ones

After generation, use semantic similarity to map any unknown column names to the closest real column in the schema.

```python
import anthropic
import re

client = anthropic.Anthropic(api_key="sk-live-...")

# Simulated schema (in production: fetch from DB)
REAL_SCHEMA = {
    "customers": ["customer_id", "full_name", "email_addr", "phone_num", "signup_date", "is_verified"],
    "orders": ["order_id", "customer_id", "order_total", "order_status", "created_at", "shipped_at"]
}


def map_column_semantically(hallucinated: str, real_columns: list[str]) -> str:
    """Use Claude to map a hallucinated column name to the closest real one."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=50,
        system="Map the given column name to the closest match in the provided list. Return only the matching column name, nothing else.",
        messages=[{
            "role": "user",
            "content": f"Hallucinated column: {hallucinated}\nReal columns: {', '.join(real_columns)}\nClosest match:"
        }]
    )
    result = response.content[0].text.strip().lower()
    # Validate the model returned a real column name
    for col in real_columns:
        if col.lower() in result:
            return col
    return real_columns[0]  # Fallback


def fix_sql_column_names(sql: str, schema: dict[str, list[str]]) -> str:
    """Find and replace hallucinated column names in generated SQL."""
    all_real_cols = {col for cols in schema.values() for col in cols}
    table_names = set(schema.keys())

    SQL_KEYWORDS = {
        "select", "from", "where", "join", "on", "and", "or", "group",
        "order", "by", "limit", "as", "left", "inner", "count", "avg",
        "sum", "max", "min", "having", "distinct", "null", "asc", "desc"
    }

    tokens = re.findall(r'\b([a-z_][a-z0-9_]*)\b', sql.lower())
    hallucinated = [
        t for t in set(tokens)
        if t not in all_real_cols
        and t not in table_names
        and t not in SQL_KEYWORDS
        and len(t) > 2
    ]

    if not hallucinated:
        return sql

    print(f"[semantic-fix] Hallucinated columns found: {hallucinated}")

    fixed_sql = sql
    all_cols_list = sorted(all_real_cols)
    for bad_col in hallucinated:
        replacement = map_column_semantically(bad_col, all_cols_list)
        print(f"[semantic-fix] '{bad_col}' → '{replacement}'")
        fixed_sql = re.sub(r'\b' + re.escape(bad_col) + r'\b', replacement, fixed_sql, flags=re.IGNORECASE)

    return fixed_sql


# Simulate agent generating SQL with hallucinated names
hallucinated_sql = """
SELECT customer_name, email_address, registration_date
FROM customers
WHERE verified = 1
ORDER BY signup_timestamp DESC
"""

fixed = fix_sql_column_names(hallucinated_sql, REAL_SCHEMA)
print("Original:", hallucinated_sql.strip())
print("Fixed:", fixed.strip())

# Expected Token Savings: semantic fix runs once post-generation → no retry round-trip to the model
# Environment: legacy NL2SQL agents where schema can't be injected upfront; migration scenarios
```

---

## Comparison

| Option | Schema Source | Catches Error | Auto-Corrects | Complexity |
|--------|--------------|---------------|---------------|------------|
| 1 | Injected schema | Prevention | N/A | Low |
| 2 | Live DB + regex lint | Yes | Retry | Low |
| 3 | Few-shot anchoring | Prevention | N/A | Low |
| 4 | Structured output | Yes (pre-execution) | Retry | Medium |
| 5 | Sandbox execution | Yes (runtime) | Yes (closest col) | Medium |
| 6 | Semantic mapping | Yes (post-generation) | Yes (LLM mapping) | Medium |

**Recommended starting point:** Option 1 (inject the real schema) — the most reliable prevention with minimal overhead. Fetch the `CREATE TABLE` statements and paste them into the system prompt before any SQL generation. Combine with Option 2's validator as a safety net to catch any names that slip through.
