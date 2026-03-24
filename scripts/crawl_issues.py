#!/usr/bin/env python3
"""Crawl GitHub issues and generate solution files."""

import json
import subprocess
import re
import os

SOLUTIONS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "solutions")

def run_gh(args):
    result = subprocess.run(["gh"] + args, capture_output=True, text=True)
    if result.returncode != 0:
        return []
    return json.loads(result.stdout) if result.stdout.strip() else []

def slugify(text):
    text = text.lower()
    text = re.sub(r'\[bug\]:?\s*', '', text)
    text = re.sub(r'[^a-z0-9\s-]', '', text)
    text = re.sub(r'[\s]+', '-', text.strip())
    text = re.sub(r'-+', '-', text)
    return text[:60].rstrip('-')

def categorize(title, body=""):
    text = (title + " " + body).lower()
    if any(w in text for w in ['google', 'gmail', 'gog', 'calendar', 'oauth', 'gemini']):
        return 'gog'
    if any(w in text for w in ['notion']):
        return 'notion'
    if any(w in text for w in ['telegram', 'tg bot']):
        return 'telegram'
    if any(w in text for w in ['docker', 'container', 'sandbox']):
        return 'docker'
    if any(w in text for w in ['clawhub', 'skill', 'plugin', 'openclaw']):
        return 'openclaw'
    return 'general'

def extract_solution_from_comments(issue_number, repo):
    """Fetch comments and look for solution patterns."""
    comments = run_gh([
        "api", f"repos/{repo}/issues/{issue_number}/comments",
        "--jq", ".[].body"
    ])
    if isinstance(comments, list):
        return None
    # comments is a string when using --jq
    return None

def extract_error_message(body):
    """Extract error messages from issue body."""
    if not body:
        return ""
    patterns = [
        r'```[\s\S]*?(error|Error|ERROR|exception|Exception|failed|Failed|FAILED)[\s\S]*?```',
        r'`([^`]*(?:error|Error|ERROR|exception|failed)[^`]*)`',
    ]
    for p in patterns:
        m = re.search(p, body)
        if m:
            return m.group(0)[:200]
    return ""

def generate_solution(issue, repo_name):
    """Generate a solution .md file from an issue."""
    title = issue.get('title', '').strip()
    body = issue.get('body', '') or ''
    url = issue.get('url', '')
    number = issue.get('number', 0)

    # Clean title
    clean_title = re.sub(r'\[Bug\]:?\s*', '', title, flags=re.IGNORECASE).strip()
    if not clean_title:
        return None

    slug = slugify(title)
    if not slug or len(slug) < 5:
        return None

    category = categorize(title, body)
    error_msg = extract_error_message(body)

    # Extract symptom from body (first paragraph or section)
    symptom = ""
    if body:
        # Try to get first meaningful paragraph
        lines = body.split('\n')
        for line in lines:
            line = line.strip()
            if line and not line.startswith('#') and not line.startswith('```') and len(line) > 20:
                symptom = line[:300]
                break

    # Try to find fix/solution/workaround in body
    solution = ""
    fix_patterns = [
        r'(?:fix|solution|workaround|resolved|solved)[:\s]*([\s\S]{20,500}?)(?:\n\n|\n#|$)',
        r'(?:I fixed|I solved|The fix|The solution)[:\s]*([\s\S]{20,500}?)(?:\n\n|\n#|$)',
    ]
    for p in fix_patterns:
        m = re.search(p, body, re.IGNORECASE)
        if m:
            solution = m.group(1).strip()[:400]
            break

    if not solution:
        solution = "이 이슈의 해결법은 원본 GitHub Issue를 참조하세요."

    content = f"""# {clean_title}

## 증상
{symptom if symptom else '에이전트 실행 중 아래 에러 발생.'}

{f'에러 메시지:{chr(10)}{error_msg}' if error_msg else ''}

## 원인
원본 이슈에서 확인 필요. GitHub Issue #{number} 참조.

## 해결법
{solution}

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: {category}
- OS: 다양

## 출처
{url if url else f'https://github.com/{repo_name}/issues/{number}'}
"""
    return {
        'slug': slug,
        'category': category,
        'content': content,
    }


def main():
    solutions = []

    # 1. openclaw/openclaw bug issues
    print("Fetching openclaw/openclaw bug issues...")
    bugs = run_gh([
        "issue", "list", "-R", "openclaw/openclaw",
        "--label", "bug", "--state", "all", "--limit", "200",
        "--json", "number,title,body,url"
    ])
    print(f"  Got {len(bugs)} bug issues")

    for issue in bugs:
        sol = generate_solution(issue, "openclaw/openclaw")
        if sol:
            solutions.append(sol)

    # 2. openclaw/openclaw other error-related issues
    print("Fetching openclaw/openclaw error-related issues...")
    for keyword in ["error", "crash", "fail", "timeout", "permission denied"]:
        issues = run_gh([
            "issue", "list", "-R", "openclaw/openclaw",
            "--search", keyword, "--state", "all", "--limit", "50",
            "--json", "number,title,body,url"
        ])
        for issue in issues:
            sol = generate_solution(issue, "openclaw/openclaw")
            if sol:
                solutions.append(sol)

    # 3. openclaw/clawhub issues
    print("Fetching openclaw/clawhub issues...")
    clawhub = run_gh([
        "issue", "list", "-R", "openclaw/clawhub",
        "--state", "all", "--limit", "200",
        "--json", "number,title,body,url"
    ])
    print(f"  Got {len(clawhub)} clawhub issues")

    for issue in clawhub:
        sol = generate_solution(issue, "openclaw/clawhub")
        if sol:
            solutions.append(sol)

    # Deduplicate by slug
    seen = set()
    unique = []
    for sol in solutions:
        if sol['slug'] not in seen:
            seen.add(sol['slug'])
            unique.append(sol)

    print(f"\nTotal unique solutions: {len(unique)}")

    # Write files
    written = 0
    for sol in unique:
        cat_dir = os.path.join(SOLUTIONS_DIR, sol['category'])
        os.makedirs(cat_dir, exist_ok=True)
        filepath = os.path.join(cat_dir, f"{sol['slug']}.md")

        if not os.path.exists(filepath):
            with open(filepath, 'w') as f:
                f.write(sol['content'])
            written += 1

    print(f"Written {written} new solution files")

    # Print stats per category
    for cat in ['openclaw', 'gog', 'notion', 'telegram', 'docker', 'general']:
        cat_dir = os.path.join(SOLUTIONS_DIR, cat)
        if os.path.exists(cat_dir):
            count = len([f for f in os.listdir(cat_dir) if f.endswith('.md')])
            print(f"  {cat}: {count} solutions")


if __name__ == "__main__":
    main()
