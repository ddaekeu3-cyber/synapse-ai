#!/usr/bin/env python3
"""Generate solutions.json search index for static API."""
import os, json, re
from datetime import datetime, timezone

BASE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "_solutions")
SITE_BASE = "/synapse-ai/solutions"

def extract_section(content, section_names):
    for name in section_names:
        m = re.search(rf'## {re.escape(name)}\n(.*?)(?=\n## |\Z)', content, re.DOTALL)
        if m:
            text = m.group(1).strip()
            # Strip markdown code blocks and excessive whitespace
            text = re.sub(r'```.*?```', '', text, flags=re.DOTALL)
            text = re.sub(r'\s+', ' ', text).strip()
            return text[:300]
    return ""

solutions = []
for root, dirs, files in os.walk(BASE):
    dirs.sort()
    for f in sorted(files):
        if not f.endswith('.md') or f in ('README.md', 'TEMPLATE.md'): continue
        path = os.path.join(root, f)
        with open(path) as fp: content = fp.read()
        cat = os.path.basename(root)
        slug = f.replace('.md', '')
        url = f"{SITE_BASE}/{cat}/{slug}"

        # Parse frontmatter title
        title_m = re.search(r'^title:\s*["\']?(.*?)["\']?\s*$', content, re.MULTILINE)
        title = title_m.group(1).strip().strip('"\'') if title_m else slug

        # Skip non-English (Chinese, Japanese, Korean title chars)
        if any(ord(ch) > 0x4DFF and ord(ch) < 0xA000 for ch in title[:100]):
            continue

        symptom = extract_section(content, ['증상', 'Symptom', 'Problem'])
        fix = extract_section(content, ['해결법', 'Fix', 'Solution'])
        source_m = re.search(r'^source:\s*(\S+)', content, re.MULTILINE)
        source = source_m.group(1) if source_m else ""

        solutions.append({
            "title": title,
            "category": cat,
            "url": url,
            "symptom": symptom,
            "fix": fix[:200],
            "source": source,
        })

out = {
    "generated": datetime.now(timezone.utc).isoformat(),
    "count": len(solutions),
    "base_url": "https://ddaekeu3-cyber.github.io",
    "usage": "Fetch this file, filter by title/symptom keywords matching your error. Example: solutions where 'rate limit' in title or symptom.",
    "solutions": solutions,
}

outpath = os.path.join(os.path.dirname(BASE), "solutions.json")
with open(outpath, 'w') as fp:
    json.dump(out, fp, ensure_ascii=False, indent=2)

print(f"Generated {len(solutions)} solutions -> solutions.json ({os.path.getsize(outpath)//1024}KB)")
