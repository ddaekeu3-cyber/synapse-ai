#!/usr/bin/env python3
"""Cleanup solutions: fix shallow content, Chinese titles, miscategorization, duplicates."""

import os
import re
import json
import subprocess
import hashlib

SOLUTIONS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "_solutions")

# Better category keywords for reclassification
CATEGORY_RULES = {
    'openclaw': [
        'openclaw', 'clawhub', 'claw', 'skill', 'plugin', 'gateway', 'mcp server',
        'mcp tool', 'mcp config', 'agent run', 'openclaw control', 'openclaw ui',
        'openclaw memory', 'openclaw setup', 'openclaw install', 'openclaw config',
        'openclaw error', 'openclaw crash', 'openclaw bug', 'openclaw gateway',
        'v2026', 'heartbeat', 'session', 'extension', 'browser tool',
        'tool execution', 'subagent', 'anthropic', 'claude', 'gemini provider',
        'llm provider', 'model provider', 'api key', 'oauth token',
        'clawhub publish', 'skill install', 'skill flagged', 'suspicious',
    ],
    'gog': [
        'google calendar', 'google drive', 'google sheets', 'google docs',
        'google api', 'google oauth', 'google workspace', 'gmail api',
        'gcp', 'google cloud', 'firebase', 'google maps',
    ],
    'telegram': [
        'telegram', 'tg bot', 'telegram bot', 'telegram api', 'telegram webhook',
        'telegram message', 'telegram group', 'telegram channel',
    ],
    'docker': [
        'docker', 'container', 'dockerfile', 'docker-compose', 'podman',
        'kubernetes', 'k8s', 'sandbox',
    ],
    'notion': [
        'notion api', 'notion database', 'notion page', 'notion integration',
    ],
    'token-cost': [
        'token cost', 'token waste', 'expensive', 'billing', 'pricing',
        'monthly cost', 'api cost', 'save token', 'reduce cost',
        'token budget', 'spending',
    ],
    'context-window': [
        'context window', 'context length', 'context overflow', 'context limit',
        'max tokens', 'input too long', 'truncat', 'conversation length',
    ],
    'hallucination': [
        'hallucin', 'making things up', 'fabricat', 'false information',
        'wrong answer', 'incorrect output', 'inaccurate', 'confabulat',
    ],
    'memory': [
        'forget', 'amnesia', 'memory loss', 'lost context', 'doesn\'t remember',
        'memory reset', 'persistent memory', 'session memory',
    ],
    'tool-failure': [
        'tool fail', 'tool error', 'function call fail', 'tool call fail',
        'mcp error', 'plugin crash',
    ],
    'rate-limit': [
        'rate limit', 'rate-limit', '429', 'too many requests', 'throttl',
        'quota exceeded', 'backoff', 'cooldown',
    ],
    'auth': [
        'oauth', 'api key invalid', 'token expired', 'unauthorized', '401',
        '403', 'permission denied', 'credential', 'login fail', 'auth fail',
    ],
    'config': [
        'misconfigur', 'wrong setting', 'env var', 'environment variable',
        'yaml config', 'json config', '.env',
    ],
    'performance': [
        'slow', 'latency', 'performance', 'timeout', 'hang', 'freeze',
        'unresponsive', 'bottleneck', 'speed',
    ],
    'loop-stuck': [
        'stuck in loop', 'infinite loop', 'retry loop', 'keeps retrying',
        'repeating error', 'same error again', 'crash loop',
    ],
    'prompt-engineering': [
        'prompt injection', 'jailbreak', 'prompt leak', 'system prompt',
    ],
    'concurrency': [
        'race condition', 'deadlock', 'concurrency', 'mutex', 'lock contention',
    ],
    'general': [],
}

# ---- Helpers ----

def read_file(path):
    with open(path, 'r', errors='replace') as f:
        return f.read()

def write_file(path, content):
    with open(path, 'w') as f:
        f.write(content)

def has_chinese(text):
    return bool(re.search(r'[\u4e00-\u9fff\u3400-\u4dbf]', text))

def classify(title, content):
    text = f"{title} {content}".lower()
    scores = {}
    for cat, keywords in CATEGORY_RULES.items():
        if cat == 'general':
            continue
        scores[cat] = 0
        for kw in keywords:
            if kw in text:
                scores[cat] += len(kw)  # longer match = more specific = higher score
    if not scores or max(scores.values()) == 0:
        return 'general'
    return max(scores, key=scores.get)

def extract_title(content):
    m = re.search(r'^title:\s*"(.+?)"', content, re.MULTILINE)
    if m:
        return m.group(1)
    m = re.search(r'^#\s+(.+)$', content, re.MULTILINE)
    if m:
        return m.group(1)
    return ""

def extract_source_url(content):
    """Extract GitHub issue URL from solution."""
    m = re.search(r'(https://github\.com/[^\s]+/issues/\d+)', content)
    if m:
        return m.group(1)
    return None

def content_hash(content):
    """Hash the core content for dedup (strip frontmatter)."""
    # Remove frontmatter
    stripped = re.sub(r'^---[\s\S]*?---\s*', '', content)
    # Remove whitespace variations
    stripped = re.sub(r'\s+', ' ', stripped).strip()
    return hashlib.md5(stripped[:500].encode()).hexdigest()

def is_meaningful(content):
    """Check if solution has meaningful content beyond boilerplate."""
    stripped = re.sub(r'^---[\s\S]*?---\s*', '', content)
    # Remove headings
    stripped = re.sub(r'^#+\s+.+$', '', stripped, flags=re.MULTILINE)
    # Remove standard boilerplate
    boilerplate = [
        '예상 토큰 절약', '이 에러로 삽질 시', '이 해결법 참조 시',
        '환경', 'OpenClaw 버전', '관련 스킬', '출처',
        'Moltbook 커뮤니티에서 보고된 문제',
        'Moltbook 댓글', 'Moltbook 포스트',
    ]
    for bp in boilerplate:
        stripped = stripped.replace(bp, '')

    meaningful = stripped.strip()
    # If less than 100 chars of unique content, it's not meaningful
    return len(meaningful) > 100


# ---- Fix 1: Shallow solutions ----

def fix_shallow_solutions():
    print("\n=== Fix 1: Shallow solutions ===")
    shallow_marker = "원본 이슈에서 확인 필요"
    fallback_marker = "이 이슈의 해결법은 원본 GitHub Issue를 참조하세요"

    deleted = 0
    kept = 0

    for root, dirs, files in os.walk(SOLUTIONS_DIR):
        for f in files:
            if not f.endswith('.md') or f in ('README.md', 'TEMPLATE.md'):
                continue
            filepath = os.path.join(root, f)
            content = read_file(filepath)

            if shallow_marker not in content and fallback_marker not in content:
                continue

            # Check if there's a GitHub URL we could fetch
            url = extract_source_url(content)

            # These are all from the initial crawl with shallow content
            # Delete them - quality over quantity
            os.remove(filepath)
            deleted += 1

    print(f"  Deleted {deleted} shallow solutions")
    return deleted


# ---- Fix 2: Chinese solutions ----

def fix_chinese_solutions():
    print("\n=== Fix 2: Chinese solutions ===")
    deleted = 0

    for root, dirs, files in os.walk(SOLUTIONS_DIR):
        for f in files:
            if not f.endswith('.md') or f in ('README.md', 'TEMPLATE.md'):
                continue
            filepath = os.path.join(root, f)
            content = read_file(filepath)
            title = extract_title(content)

            # Check if title or first 500 chars of content is primarily Chinese
            check_text = f"{title} {content[:500]}"
            chinese_chars = len(re.findall(r'[\u4e00-\u9fff\u3400-\u4dbf]', check_text))
            total_chars = len(check_text)

            if total_chars > 0 and chinese_chars / total_chars > 0.15:
                # More than 15% Chinese characters - delete
                os.remove(filepath)
                deleted += 1

    print(f"  Deleted {deleted} Chinese-heavy solutions")
    return deleted


# ---- Fix 3: Reclassify ----

def fix_reclassify():
    print("\n=== Fix 3: Reclassify miscategorized solutions ===")
    moved = 0

    for root, dirs, files in os.walk(SOLUTIONS_DIR):
        for f in files:
            if not f.endswith('.md') or f in ('README.md', 'TEMPLATE.md', '.gitkeep'):
                continue
            filepath = os.path.join(root, f)
            content = read_file(filepath)
            title = extract_title(content)

            current_cat = os.path.basename(root)
            correct_cat = classify(title, content)

            if correct_cat != current_cat and correct_cat != 'general':
                # Move to correct category
                new_dir = os.path.join(SOLUTIONS_DIR, correct_cat)
                os.makedirs(new_dir, exist_ok=True)
                new_path = os.path.join(new_dir, f)

                # Update frontmatter category
                content = re.sub(
                    r'^category:\s*.+$',
                    f'category: {correct_cat}',
                    content, count=1, flags=re.MULTILINE
                )

                # Handle name collision
                if os.path.exists(new_path):
                    continue

                write_file(new_path, content)
                os.remove(filepath)
                moved += 1

    print(f"  Moved {moved} solutions to correct categories")
    return moved


# ---- Fix 4: Quality check (dedup, spam, meaningless) ----

def fix_quality():
    print("\n=== Fix 4: Quality check ===")

    # Collect all solutions
    solutions = {}
    for root, dirs, files in os.walk(SOLUTIONS_DIR):
        for f in files:
            if not f.endswith('.md') or f in ('README.md', 'TEMPLATE.md', '.gitkeep'):
                continue
            filepath = os.path.join(root, f)
            content = read_file(filepath)
            solutions[filepath] = content

    # Pass 1: Remove duplicates (same content hash)
    hashes = {}
    dup_deleted = 0
    for filepath, content in list(solutions.items()):
        h = content_hash(content)
        if h in hashes:
            os.remove(filepath)
            del solutions[filepath]
            dup_deleted += 1
        else:
            hashes[h] = filepath

    print(f"  Removed {dup_deleted} duplicates")

    # Pass 2: Remove meaningless solutions
    meaningless_deleted = 0
    for filepath, content in list(solutions.items()):
        if not is_meaningful(content):
            os.remove(filepath)
            del solutions[filepath]
            meaningless_deleted += 1

    print(f"  Removed {meaningless_deleted} meaningless solutions")

    # Pass 3: Remove spam patterns
    spam_patterns = [
        r'mbc-20.*mint',
        r'mbc20\.xyz',
        r'follow me',
        r'subscribe.*channel',
        r'join.*discord',
    ]
    spam_deleted = 0
    for filepath, content in list(solutions.items()):
        for sp in spam_patterns:
            if re.search(sp, content, re.IGNORECASE):
                os.remove(filepath)
                del solutions[filepath]
                spam_deleted += 1
                break

    print(f"  Removed {spam_deleted} spam solutions")

    return dup_deleted + meaningless_deleted + spam_deleted


# ---- Main ----

def main():
    print("Starting cleanup...")

    # Count before
    before = sum(1 for r, d, f in os.walk(SOLUTIONS_DIR)
                 for fn in f if fn.endswith('.md') and fn not in ('README.md', 'TEMPLATE.md'))
    print(f"Solutions before cleanup: {before}")

    d1 = fix_shallow_solutions()
    d2 = fix_chinese_solutions()
    fix_reclassify()
    d4 = fix_quality()

    # Clean empty .gitkeep if folder has other files
    for cat_dir in os.listdir(SOLUTIONS_DIR):
        cat_path = os.path.join(SOLUTIONS_DIR, cat_dir)
        if os.path.isdir(cat_path):
            files = os.listdir(cat_path)
            md_files = [f for f in files if f.endswith('.md')]
            if md_files and '.gitkeep' in files:
                os.remove(os.path.join(cat_path, '.gitkeep'))

    # Count after
    after = sum(1 for r, d, f in os.walk(SOLUTIONS_DIR)
                for fn in f if fn.endswith('.md') and fn not in ('README.md', 'TEMPLATE.md'))

    print(f"\n=== Summary ===")
    print(f"Before: {before}")
    print(f"After: {after}")
    print(f"Removed: {before - after}")

    print(f"\nCategory distribution:")
    for cat_dir in sorted(os.listdir(SOLUTIONS_DIR)):
        cat_path = os.path.join(SOLUTIONS_DIR, cat_dir)
        if os.path.isdir(cat_path):
            count = len([f for f in os.listdir(cat_path) if f.endswith('.md')])
            if count > 0:
                print(f"  {cat_dir}: {count}")


if __name__ == "__main__":
    main()
