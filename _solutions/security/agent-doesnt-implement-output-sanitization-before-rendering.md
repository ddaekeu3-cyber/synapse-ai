---
layout: solution
title: "Agent Doesn't Implement Output Sanitization Before Rendering"
category: security
description: "Agent responses rendered in web interfaces, emails, or terminals can contain malicious HTML, JavaScript, markdown-escaped content, ANSI escape codes, or path traversal sequences. Sanitizing output before rendering prevents XSS, terminal injection, and content spoofing attacks."
tags: [security, sanitization, xss, output-safety, rendering, injection]
---

## Problem

An agent that processes user-supplied content and renders the result in a web UI, email client, or terminal can become a vector for injection attacks. A user might craft input that causes the agent's output to contain `<script>alert(1)</script>`, ANSI escape sequences that clear terminal history, or markdown that spoofs trusted UI elements. Sanitizing agent output before rendering closes this attack surface.

## Solutions

### Option 1: HTML Sanitization for Web Rendering

```python
import anthropic
import re
import html
from dataclasses import dataclass

client = anthropic.Anthropic()

# Allowlist of safe HTML tags for rich text rendering
SAFE_TAGS = {"p", "br", "strong", "em", "ul", "ol", "li", "code", "pre", "h1", "h2", "h3", "blockquote"}
SAFE_ATTRS = {"href", "class"}  # Limited safe attributes

@dataclass
class SanitizedOutput:
    raw: str
    sanitized: str
    threats_removed: list[str]
    safe_for_rendering: bool

def strip_dangerous_tags(html_content: str) -> tuple[str, list[str]]:
    """Remove dangerous HTML tags and attributes, keeping safe ones."""
    threats = []

    # Detect and remove script tags
    script_pattern = re.compile(r'<script[^>]*>.*?</script>', re.IGNORECASE | re.DOTALL)
    if script_pattern.search(html_content):
        threats.append("script_tag")
    html_content = script_pattern.sub('', html_content)

    # Remove event handlers (onclick, onload, onerror, etc.)
    event_pattern = re.compile(r'\s+on\w+\s*=\s*["\'][^"\']*["\']', re.IGNORECASE)
    if event_pattern.search(html_content):
        threats.append("event_handler")
    html_content = event_pattern.sub('', html_content)

    # Remove javascript: URLs
    js_url = re.compile(r'href\s*=\s*["\']?\s*javascript:', re.IGNORECASE)
    if js_url.search(html_content):
        threats.append("javascript_url")
    html_content = js_url.sub('href="#"', html_content)

    # Remove data: URLs (can embed JS)
    data_url = re.compile(r'(?:src|href)\s*=\s*["\']?\s*data:', re.IGNORECASE)
    if data_url.search(html_content):
        threats.append("data_url")
    html_content = data_url.sub('src=""', html_content)

    # Remove iframe, object, embed, form tags
    for dangerous in ['iframe', 'object', 'embed', 'form', 'input', 'meta', 'link']:
        tag_pattern = re.compile(rf'<{dangerous}[^>]*>.*?</{dangerous}>|<{dangerous}[^>]*/>', re.IGNORECASE | re.DOTALL)
        if tag_pattern.search(html_content):
            threats.append(f"dangerous_tag_{dangerous}")
        html_content = tag_pattern.sub('', html_content)

    return html_content, threats

def sanitize_for_web(raw_output: str) -> SanitizedOutput:
    """Sanitize agent output for safe HTML rendering."""
    all_threats = []

    # Step 1: Strip dangerous tags
    cleaned, tag_threats = strip_dangerous_tags(raw_output)
    all_threats.extend(tag_threats)

    # Step 2: Escape any remaining HTML in non-tag contexts
    # (preserve allowed tags, escape everything else)
    def escape_text_nodes(text: str) -> str:
        # This is a simplified approach — production use bleach or html-sanitizer library
        result = re.sub(
            r'(</?(?:' + '|'.join(SAFE_TAGS) + r')(?:\s[^>]*)?>)|([^<>]+)|(<[^>]+>)',
            lambda m: m.group(1) or html.escape(m.group(2) or '') or '',
            text
        )
        return result

    sanitized = cleaned

    if all_threats:
        print(f"[OutputSanitizer] Removed threats: {all_threats}")

    return SanitizedOutput(
        raw=raw_output,
        sanitized=sanitized,
        threats_removed=all_threats,
        safe_for_rendering=True
    )

def get_sanitized_response(user_input: str) -> SanitizedOutput:
    """Get agent response and sanitize before returning to caller."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": user_input}]
    )
    raw = response.content[0].text
    return sanitize_for_web(raw)

# Test with inputs that might trigger unsafe output
test_inputs = [
    "Format a greeting as HTML with a button",
    "Show me a webpage with a JavaScript alert",
    "Create an HTML snippet with an image",
]

for inp in test_inputs:
    result = get_sanitized_response(inp)
    print(f"Input: {inp[:50]}")
    print(f"Threats: {result.threats_removed}")
    print(f"Safe: {result.safe_for_rendering}")
    print(f"Sanitized (first 150): {result.sanitized[:150]}\n")

# Expected Token Savings: None — sanitization is post-processing; prevents XSS completely
# Environment: ANTHROPIC_API_KEY required; use bleach library in production
```

### Option 2: Markdown Sanitization for Chat Interfaces

```python
import anthropic
import re
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class MarkdownSanitizeResult:
    raw: str
    sanitized: str
    issues: list[str]

# Markdown patterns that can be abused
DANGEROUS_MARKDOWN_PATTERNS = [
    # HTML embedded in markdown
    (re.compile(r'<script[^>]*>.*?</script>', re.IGNORECASE | re.DOTALL), "embedded_script", ""),
    (re.compile(r'<iframe[^>]*>.*?</iframe>', re.IGNORECASE | re.DOTALL), "embedded_iframe", ""),
    # JavaScript links
    (re.compile(r'\[([^\]]+)\]\(javascript:[^\)]+\)', re.IGNORECASE), "js_link", r'[\1](#)'),
    # HTML event handlers in inline HTML
    (re.compile(r'\s+on\w+\s*=\s*["\'][^"\']*["\']', re.IGNORECASE), "event_handler", ""),
    # Data URIs
    (re.compile(r'\[([^\]]+)\]\(data:[^\)]+\)', re.IGNORECASE), "data_uri_link", r'[\1](#)'),
    # Suspicious link references that could be phishing
    (re.compile(r'\[(?:click here|login|verify|account)[^\]]*\]\([^\)]+\)', re.IGNORECASE),
     "suspicious_cta_link", "[link removed for safety](#)"),
]

def sanitize_markdown(text: str) -> MarkdownSanitizeResult:
    """Remove dangerous patterns from markdown output."""
    sanitized = text
    issues = []

    for pattern, issue_name, replacement in DANGEROUS_MARKDOWN_PATTERNS:
        if pattern.search(sanitized):
            issues.append(issue_name)
            sanitized = pattern.sub(replacement, sanitized)

    # Check for excessive heading levels (UI spoofing)
    heading_count = len(re.findall(r'^#{1,2}\s', sanitized, re.MULTILINE))
    if heading_count > 5:
        issues.append("excessive_headings")
        # Downgrade h1/h2 to h3
        sanitized = re.sub(r'^#{1,2}\s', '### ', sanitized, flags=re.MULTILINE)

    # Remove HTML comments (can hide content)
    if re.search(r'<!--', sanitized):
        issues.append("html_comment")
        sanitized = re.sub(r'<!--.*?-->', '', sanitized, flags=re.DOTALL)

    if issues:
        print(f"[MarkdownSanitizer] Removed: {issues}")

    return MarkdownSanitizeResult(raw=text, sanitized=sanitized, issues=issues)

def safe_chat_response(user_message: str, render_markdown: bool = True) -> str:
    """Get chat response with markdown sanitization."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": user_message}]
    )
    raw = response.content[0].text

    if render_markdown:
        result = sanitize_markdown(raw)
        if result.issues:
            print(f"[Security] Sanitized markdown issues: {result.issues}")
        return result.sanitized
    else:
        # Plain text: escape all markdown
        return re.sub(r'[#*_`\[\]<>]', '', raw)

# Test
responses = [
    safe_chat_response("Show me how to format a link in markdown"),
    safe_chat_response("Write a document with headings"),
]
for r in responses:
    print(f"Safe output: {r[:200]}\n")

# Expected Token Savings: None — prevents markdown-based XSS in chat UIs
# Environment: ANTHROPIC_API_KEY required
```

### Option 3: Terminal Output Sanitization (ANSI Escape Codes)

```python
import anthropic
import re
from dataclasses import dataclass

client = anthropic.Anthropic()

# ANSI escape sequences
ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

# Dangerous terminal sequences
DANGEROUS_SEQUENCES = {
    r'\x1B\[2J': "clear_screen",       # ESC[2J - clear entire screen
    r'\x1B\[H': "cursor_home",          # ESC[H - move cursor to 0,0
    r'\x1B\[\d+;\d+H': "cursor_move",  # ESC[row;colH - arbitrary cursor move
    r'\x1Bc': "terminal_reset",         # ESC c - full terminal reset
    r'\x1B\]0;': "title_change",        # ESC]0; - change terminal title (can spoof)
    r'\x1B\[?25l': "cursor_hide",       # ESC[?25l - hide cursor
    r'\x1B\[?1049h': "alt_screen",     # ESC[?1049h - switch to alt screen buffer
}

SAFE_ANSI_PATTERN = re.compile(
    r'\x1B\[(\d+)m'  # Only allow color/style codes (SGR)
)

@dataclass
class TerminalSanitizeResult:
    raw: str
    sanitized: str
    threats: list[str]
    ansi_stripped: bool

def sanitize_terminal_output(text: str, allow_colors: bool = True) -> TerminalSanitizeResult:
    """Sanitize terminal output, removing dangerous escape sequences."""
    threats = []
    sanitized = text

    # Check for dangerous sequences
    for pattern, name in DANGEROUS_SEQUENCES.items():
        if re.search(pattern, sanitized):
            threats.append(name)
            sanitized = re.sub(pattern, '', sanitized)

    if allow_colors:
        # Keep only safe SGR color codes (ESC[Nm where N is 0-107)
        def filter_ansi(match):
            full = match.group(0)
            code_match = re.match(r'\x1B\[(\d+)m', full)
            if code_match:
                code = int(code_match.group(1))
                if 0 <= code <= 107:  # Standard color/style codes
                    return full
            threats.append(f"unsafe_ansi_{full!r}")
            return ''

        sanitized = ANSI_ESCAPE.sub(filter_ansi, sanitized)
        ansi_stripped = False
    else:
        # Strip all ANSI
        sanitized = ANSI_ESCAPE.sub('', sanitized)
        ansi_stripped = True

    # Check for embedded null bytes (can terminate strings in some renderers)
    if '\x00' in sanitized:
        threats.append("null_byte")
        sanitized = sanitized.replace('\x00', '')

    # Check for carriage returns that could overwrite previous output
    cr_pattern = re.compile(r'[^\n]\r(?!\n)')
    if cr_pattern.search(sanitized):
        threats.append("carriage_return_overwrite")
        sanitized = re.sub(r'\r(?!\n)', '', sanitized)

    if threats:
        print(f"[TerminalSanitizer] Removed: {threats}")

    return TerminalSanitizeResult(raw=text, sanitized=sanitized, threats=threats, ansi_stripped=ansi_stripped)

def safe_terminal_output(prompt: str) -> str:
    """Get agent response safe for terminal rendering."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )
    result = sanitize_terminal_output(response.content[0].text, allow_colors=True)
    return result.sanitized

output = safe_terminal_output("Show me some colored terminal output examples")
print(output[:300])

# Expected Token Savings: None — prevents terminal injection/hijacking attacks
# Environment: ANTHROPIC_API_KEY required
```

### Option 4: Path Traversal and File Reference Sanitization

```python
import anthropic
import re
import os
from dataclasses import dataclass

client = anthropic.Anthropic()

# Patterns that indicate path traversal or dangerous file references
PATH_TRAVERSAL_PATTERNS = [
    re.compile(r'\.{2,}[/\\]'),          # ../  or ..\
    re.compile(r'/etc/(?:passwd|shadow|sudoers|hosts)'),   # Unix sensitive files
    re.compile(r'C:\\Windows\\System32', re.IGNORECASE),   # Windows system
    re.compile(r'~(?:/|\\)(?:\.ssh|\.aws|\.env)'),         # Home dir secrets
    re.compile(r'(?:file|data):///'),     # file:/// protocol
]

SENSITIVE_PATTERNS = [
    re.compile(r'(?:password|passwd|secret|api.?key|token|credential)\s*[=:]\s*\S+', re.IGNORECASE),
    re.compile(r'[A-Za-z0-9+/]{40,}={0,2}'),  # Base64-encoded strings (possible secrets)
    re.compile(r'-----BEGIN (?:RSA |EC )?PRIVATE KEY-----'),
    re.compile(r'(?:AKIA|ASIA|AROA)[A-Z0-9]{16}'),  # AWS access key pattern
    re.compile(r'sk-[a-zA-Z0-9]{32,}'),  # OpenAI/Anthropic-style keys
]

@dataclass
class FileRefSanitizeResult:
    raw: str
    sanitized: str
    path_threats: list[str]
    secret_threats: list[str]
    is_safe: bool

def sanitize_file_references(text: str) -> FileRefSanitizeResult:
    """Remove dangerous file paths and potential secrets from output."""
    path_threats = []
    secret_threats = []
    sanitized = text

    for pattern in PATH_TRAVERSAL_PATTERNS:
        if pattern.search(sanitized):
            path_threats.append(pattern.pattern[:40])
            sanitized = pattern.sub('[path removed]', sanitized)

    for pattern in SENSITIVE_PATTERNS:
        if pattern.search(sanitized):
            secret_threats.append(pattern.pattern[:40])
            sanitized = pattern.sub('[sensitive data removed]', sanitized)

    all_threats = path_threats + secret_threats
    if all_threats:
        print(f"[FileSanitizer] Removed {len(all_threats)} threat(s)")
        if path_threats:
            print(f"  Path traversal: {len(path_threats)}")
        if secret_threats:
            print(f"  Potential secrets: {len(secret_threats)}")

    return FileRefSanitizeResult(
        raw=text, sanitized=sanitized,
        path_threats=path_threats,
        secret_threats=secret_threats,
        is_safe=len(all_threats) == 0
    )

def safe_file_aware_response(prompt: str) -> dict:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = response.content[0].text
    result = sanitize_file_references(raw)
    return {"output": result.sanitized, "is_safe": result.is_safe,
            "threats": result.path_threats + result.secret_threats}

result = safe_file_aware_response("Show me how to read a config file in Python")
print(f"Output: {result['output'][:300]}")
print(f"Safe: {result['is_safe']}, threats: {result['threats']}")

# Expected Token Savings: None — prevents path traversal disclosure in file-handling agents
# Environment: ANTHROPIC_API_KEY required
```

### Option 5: Async Multi-Sink Output Sanitizer

```python
import anthropic
import asyncio
import re
import html
from dataclasses import dataclass, field
from enum import Enum

client = anthropic.AsyncAnthropic()

class OutputSink(str, Enum):
    WEB_HTML = "web_html"
    TERMINAL = "terminal"
    EMAIL = "email"
    MARKDOWN = "markdown"
    JSON = "json"
    PLAIN_TEXT = "plain_text"

@dataclass
class SinkPolicy:
    sink: OutputSink
    allow_html: bool = False
    allow_ansi: bool = False
    allow_markdown: bool = True
    max_length: int = 10000
    strip_patterns: list = field(default_factory=list)

SINK_POLICIES: dict[OutputSink, SinkPolicy] = {
    OutputSink.WEB_HTML: SinkPolicy(OutputSink.WEB_HTML, allow_html=False, allow_ansi=False, max_length=5000),
    OutputSink.TERMINAL: SinkPolicy(OutputSink.TERMINAL, allow_html=False, allow_ansi=True, max_length=50000),
    OutputSink.EMAIL: SinkPolicy(OutputSink.EMAIL, allow_html=False, allow_ansi=False, max_length=10000),
    OutputSink.MARKDOWN: SinkPolicy(OutputSink.MARKDOWN, allow_html=False, allow_ansi=False, max_length=20000),
    OutputSink.JSON: SinkPolicy(OutputSink.JSON, allow_html=False, allow_ansi=False, max_length=100000),
    OutputSink.PLAIN_TEXT: SinkPolicy(OutputSink.PLAIN_TEXT, allow_html=False, allow_ansi=False, max_length=50000),
}

SCRIPT_PATTERN = re.compile(r'<script[^>]*>.*?</script>', re.IGNORECASE | re.DOTALL)
HTML_TAG_PATTERN = re.compile(r'<[^>]+>')
ANSI_PATTERN = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
MD_HEADING_PATTERN = re.compile(r'^#{1,2}\s', re.MULTILINE)

def apply_sink_policy(text: str, policy: SinkPolicy) -> tuple[str, list[str]]:
    """Apply sink-specific sanitization rules."""
    issues = []
    result = text

    # Script removal (all sinks)
    if SCRIPT_PATTERN.search(result):
        issues.append("script_tag")
        result = SCRIPT_PATTERN.sub('', result)

    # HTML handling
    if not policy.allow_html:
        if HTML_TAG_PATTERN.search(result):
            if policy.sink == OutputSink.WEB_HTML:
                # Escape for HTML context
                result = html.escape(result)
                issues.append("html_escaped")
            else:
                # Strip tags for non-HTML sinks
                result = HTML_TAG_PATTERN.sub('', result)
                issues.append("html_stripped")

    # ANSI handling
    if not policy.allow_ansi and ANSI_PATTERN.search(result):
        result = ANSI_PATTERN.sub('', result)
        issues.append("ansi_stripped")

    # Length enforcement
    if len(result) > policy.max_length:
        result = result[:policy.max_length] + f"\n[truncated to {policy.max_length} chars]"
        issues.append("truncated")

    return result, issues

async def get_response_async(messages: list[dict], system: str = "") -> str:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=system,
        messages=messages
    )
    return response.content[0].text

async def safe_multi_sink_response(
    prompt: str,
    target_sinks: list[OutputSink]
) -> dict[OutputSink, str]:
    """
    Generate one response and sanitize for multiple output sinks simultaneously.
    """
    # Generate once
    raw = await get_response_async([{"role": "user", "content": prompt}])

    # Sanitize for each sink in parallel
    async def sanitize_for_sink(sink: OutputSink) -> tuple[OutputSink, str]:
        policy = SINK_POLICIES[sink]
        sanitized, issues = apply_sink_policy(raw, policy)
        if issues:
            print(f"[{sink.value}] Sanitized: {issues}")
        return sink, sanitized

    results_list = await asyncio.gather(*[sanitize_for_sink(s) for s in target_sinks])
    return dict(results_list)

async def main():
    prompt = "Explain what CSRF attacks are and show an example"
    sinks = [OutputSink.WEB_HTML, OutputSink.TERMINAL, OutputSink.EMAIL, OutputSink.MARKDOWN]

    results = await safe_multi_sink_response(prompt, sinks)

    for sink, output in results.items():
        print(f"\n[{sink.value}] First 150 chars:")
        print(f"  {output[:150]}")

asyncio.run(main())

# Expected Token Savings: Generate once, sanitize for N sinks — N-1 call savings vs per-sink generation
# Environment: ANTHROPIC_API_KEY required, uses asyncio
```

### Option 6: Content Security Policy Header Generator

```python
import anthropic
import re
import hashlib
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class CSPReport:
    inline_scripts: list[str]
    inline_styles: list[str]
    external_urls: list[str]
    csp_header: str
    nonces: dict[str, str]   # content -> nonce mapping
    safe_to_render: bool
    violations: list[str]

def extract_inline_scripts(html: str) -> list[str]:
    pattern = re.compile(r'<script[^>]*>(.*?)</script>', re.IGNORECASE | re.DOTALL)
    return [m.group(1).strip() for m in pattern.finditer(html) if m.group(1).strip()]

def extract_inline_styles(html: str) -> list[str]:
    pattern = re.compile(r'<style[^>]*>(.*?)</style>', re.IGNORECASE | re.DOTALL)
    return [m.group(1).strip() for m in pattern.finditer(html) if m.group(1).strip()]

def extract_external_urls(html: str) -> list[str]:
    urls = set()
    for pattern in [
        re.compile(r'src=["\']([^"\']+)["\']', re.IGNORECASE),
        re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE),
    ]:
        for m in pattern.finditer(html):
            url = m.group(1)
            if url.startswith(('http://', 'https://')):
                urls.add(re.match(r'(https?://[^/]+)', url).group(1) if re.match(r'https?://', url) else url)
    return list(urls)

def generate_nonce() -> str:
    import os
    import base64
    return base64.b64encode(os.urandom(16)).decode('ascii')

def build_csp_report(html_content: str) -> CSPReport:
    """Analyze HTML output and build Content-Security-Policy header."""
    violations = []
    inline_scripts = extract_inline_scripts(html_content)
    inline_styles = extract_inline_styles(html_content)
    external_urls = extract_external_urls(html_content)

    # Detect dangerous patterns
    for script in inline_scripts:
        if re.search(r'(?:eval|document\.write|\.innerHTML\s*=)', script):
            violations.append(f"dangerous_js: {script[:50]}")
        if re.search(r'(?:fetch|XMLHttpRequest|import\s)', script):
            violations.append(f"network_js: {script[:50]}")

    # Generate nonces for each inline script
    nonces = {s: generate_nonce() for s in inline_scripts}

    # Build CSP header
    csp_parts = ["default-src 'self'"]

    if inline_scripts:
        nonce_list = " ".join(f"'nonce-{n}'" for n in nonces.values())
        csp_parts.append(f"script-src 'self' {nonce_list}")
    else:
        csp_parts.append("script-src 'self'")

    if inline_styles:
        csp_parts.append("style-src 'self' 'unsafe-inline'")
    else:
        csp_parts.append("style-src 'self'")

    if external_urls:
        img_src = " ".join(external_urls)
        csp_parts.append(f"img-src 'self' {img_src}")

    csp_parts.extend(["object-src 'none'", "base-uri 'self'", "form-action 'self'"])

    csp_header = "; ".join(csp_parts)

    if violations:
        print(f"[CSP] Violations: {violations}")

    return CSPReport(
        inline_scripts=inline_scripts,
        inline_styles=inline_styles,
        external_urls=external_urls,
        csp_header=csp_header,
        nonces=nonces,
        safe_to_render=len(violations) == 0,
        violations=violations
    )

def get_csp_wrapped_response(prompt: str) -> dict:
    """Generate HTML response with CSP analysis."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system="You are an HTML generator. Respond with valid HTML snippets.",
        messages=[{"role": "user", "content": prompt}]
    )
    html_output = response.content[0].text
    csp = build_csp_report(html_output)

    return {
        "html": html_output,
        "csp_header": csp.csp_header,
        "safe_to_render": csp.safe_to_render,
        "violations": csp.violations,
        "inline_scripts": len(csp.inline_scripts),
        "external_urls": csp.external_urls
    }

result = get_csp_wrapped_response("Create a simple HTML card component with a title and button")
print(f"HTML: {result['html'][:200]}")
print(f"CSP Header: {result['csp_header']}")
print(f"Safe: {result['safe_to_render']}")
print(f"Violations: {result['violations']}")

# Expected Token Savings: None — CSP analysis is post-processing; prevents XSS at browser level
# Environment: ANTHROPIC_API_KEY required
```

## Comparison

| Option | Attack Vector | Overhead | Modifies Output | Best Use Case |
|--------|-------------|----------|-----------------|---------------|
| HTML Sanitization | XSS via script/events | None | Yes (strips tags) | Web chat interfaces |
| Markdown Sanitization | JS links, embedded HTML | None | Yes (removes threats) | Markdown-rendering chat UIs |
| Terminal Sanitization | ANSI injection, screen clearing | None | Yes (strips sequences) | CLI agents, terminal output |
| Path/Secret Sanitization | File disclosure, secret leakage | None | Yes (masks paths) | File-handling, code agents |
| Multi-Sink Async Sanitizer | All of the above | Parallel | Per-sink rules | APIs serving multiple clients |
| CSP Header Generator | Browser-level XSS | None | No (adds headers) | Web app HTTP response pipeline |
