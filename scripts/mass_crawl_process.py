#!/usr/bin/env python3
"""Mass process crawled GitHub Issues + Reddit posts into SynapseAI solutions."""

import json
import os
import re
import glob
import hashlib

SOLUTIONS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "_solutions")
CRAWL_DIR = "/tmp/synapse-crawl"

CATEGORY_RULES = {
    'openclaw': ['openclaw', 'clawhub', 'gateway', 'skill', 'plugin', 'mcp server', 'subagent', 'extension', 'control ui', 'browser tool', 'v2026'],
    'token-cost': ['token cost', 'token waste', 'expensive', 'billing', 'pricing', 'save token', 'token budget', 'usage cost', 'token usage', 'burning money', 'token limit'],
    'context-window': ['context window', 'context length', 'context overflow', 'context limit', 'truncat', 'conversation length', 'max token', 'input too long'],
    'hallucination': ['hallucin', 'making things up', 'fabricat', 'wrong answer', 'incorrect output', 'inaccurate', 'false information', 'confabulat'],
    'memory': ['forget', 'amnesia', 'memory loss', 'lost context', 'doesn\'t remember', 'memory reset', 'persistent memory', 'memory index'],
    'tool-failure': ['tool fail', 'tool error', 'function call', 'tool call', 'mcp error', 'plugin crash', 'tool not found'],
    'rate-limit': ['rate limit', 'rate-limit', '429', 'too many requests', 'throttl', 'quota', 'cooldown', 'backoff'],
    'auth': ['oauth', 'api key', 'token expired', 'unauthorized', '401', '403', 'permission denied', 'credential', 'login', 'auth'],
    'config': ['config', 'setup', 'install', 'environment variable', 'env var', '.env', 'settings', 'misconfigur', 'yaml'],
    'performance': ['slow', 'latency', 'performance', 'timeout', 'hang', 'freeze', 'unresponsive', 'speed', 'bottleneck'],
    'loop-stuck': ['stuck', 'loop', 'infinite loop', 'retry loop', 'keeps retrying', 'repeating', 'crash loop', 'restart loop'],
    'telegram': ['telegram', 'tg bot', 'telegram bot', 'telegram api'],
    'docker': ['docker', 'container', 'dockerfile', 'podman', 'kubernetes', 'sandbox'],
    'concurrency': ['race condition', 'deadlock', 'concurrency', 'parallel', 'async', 'mutex'],
    'prompt-engineering': ['prompt injection', 'jailbreak', 'prompt leak', 'system prompt'],
    'general': [],
}

# Category-specific solution templates
SOLUTIONS_MAP = {
    'openclaw': "1. OpenClaw 최신 버전으로 업데이트: `npm update -g openclaw`\n2. Gateway 재시작: `openclaw gateway restart`\n3. 설정 파일 확인: `~/.openclaw/config.yaml`\n4. 로그 확인: `openclaw logs --tail 50`\n5. 원본 GitHub Issue에서 패치 확인",
    'token-cost': "1. 모델 선택 최적화: 단순 작업은 Haiku, 복잡한 작업만 Opus 사용\n2. 프롬프트 캐싱 활성화: 반복 시스템 프롬프트 캐싱으로 90% 절감\n3. 컨텍스트 최소화: 필요한 정보만 포함\n4. 에러 루프 방지: 3회 실패 시 다른 접근법으로 전환\n5. 토큰 사용량 모니터링 대시보드 확인",
    'context-window': "1. 대화 분할: 긴 작업은 여러 세션으로 분리\n2. 요약 활용: 이전 대화를 구조화된 요약으로 대체\n3. 선택적 컨텍스트: 관련 정보만 포함, 전체 파일 붙여넣기 금지\n4. 주기적 리프레시: 20턴마다 컨텍스트 정리\n5. 핵심 정보는 프롬프트 시작/끝에 배치",
    'hallucination': "1. 검증 루프: 생성 → 실행/확인 → 수정 → 재검증\n2. '모르면 모른다고' 시스템 프롬프트 설정\n3. RAG 활용: 외부 문서 검색 기반 답변\n4. 코드는 반드시 실행해서 검증\n5. 출력에 출처/근거 명시 요구",
    'memory': "1. 영속적 메모리 파일: CLAUDE.md에 핵심 정보 기록\n2. 세션 요약 자동 저장: 종료 시 진행상황 파일 저장\n3. 체크포인트: 장기 작업에서 주기적 상태 저장\n4. 외부 상태 관리: JSON/DB에 에이전트 상태 저장",
    'tool-failure': "1. 에러 메시지 정확히 읽기: 에러 코드로 원인 파악\n2. 권한 확인: API 키, 토큰, 스코프 확인\n3. 버전 호환성: 도구/API 버전 확인\n4. 대체 도구: 실패 시 동일 기능의 대체 도구 사용\n5. 재시도: 일시적 오류는 지수 백오프로 재시도",
    'rate-limit': "1. 지수 백오프: 1초→2초→4초→8초 재시도 간격\n2. 지터 추가: 랜덤 지터로 thundering herd 방지\n3. 캐싱: 동일 요청 결과 캐싱\n4. Retry-After 헤더 준수\n5. 배치 처리: 개별 요청을 배치로 묶기",
    'auth': "1. API 키 유효성/만료 확인\n2. OAuth 토큰 갱신: refresh token 사용\n3. 환경변수 확인: .env 파일 설정 검증\n4. 캐시된 인증 정보 삭제: `~/.openclaw/credentials.json` 제거 후 재인증\n5. IP 화이트리스트/스코프 확인",
    'config': "1. 공식 문서 참조: 최신 설정 가이드 확인\n2. 환경변수 확인: 필수 변수 설정 확인\n3. 버전 호환성: 설정 포맷이 현재 버전과 맞는지 확인\n4. 로그 확인: 시작 로그에서 설정 관련 경고 확인\n5. 최소 설정으로 시작해서 하나씩 추가",
    'performance': "1. 병목 식별: 프로파일링으로 가장 느린 부분 찾기\n2. 캐싱: 반복 연산/API 호출 캐싱\n3. 병렬 처리: 독립 작업 동시 실행\n4. 타임아웃 설정: 무한 대기 방지\n5. 리소스 모니터링: CPU, 메모리, 네트워크 확인",
    'loop-stuck': "1. 최대 재시도 제한: 동일 작업 3-5회 제한\n2. 에러 패턴 감지: 같은 에러 반복 시 다른 접근법 전환\n3. 타임아웃: 단일 작업 시간 제한 설정\n4. 상태 체크포인트: 진행상황 기록으로 반복 방지\n5. 에스컬레이션: 실패 시 사람에게 보고",
    'telegram': "1. Bot Token 확인: BotFather에서 토큰 재발급\n2. Webhook URL 설정 확인\n3. 메시지 포맷 호환성 확인\n4. Rate limit: Telegram API 제한 준수\n5. 그룹 권한 설정 확인",
    'docker': "1. 권한 확인: --user 플래그, 볼륨 권한\n2. 네트워크: 컨테이너 간 연결, DNS 확인\n3. 로그: docker logs로 에러 확인\n4. 리소스 제한: 메모리/CPU 충분한지 확인\n5. 볼륨 마운트: 경로 매핑 정확히 확인",
    'concurrency': "1. 락 사용: 공유 리소스에 적절한 락/뮤텍스\n2. 원자적 연산: 경쟁 조건 방지\n3. 큐 기반 처리: 메시지 큐로 통신\n4. 타임아웃: 락 대기에 타임아웃 설정\n5. 스트레스 테스트: 동시성 버그 발견",
    'prompt-engineering': "1. 명확한 지시: 구체적이고 명확한 표현\n2. Few-shot 예시: 원하는 출력 예시 제공\n3. 역할 지정: 시스템 프롬프트에 역할/제약 명시\n4. 출력 포맷 지정: JSON, 마크다운 등\n5. 보안: 프롬프트 인젝션 방지 입력 검증",
    'general': "1. 에러 메시지 정확히 읽기\n2. 공식 문서 확인\n3. GitHub Issues에서 유사 사례 검색\n4. 최소 재현 코드로 원인 격리\n5. SynapseAI DB에서 기존 해결법 검색",
}


def get_existing_slugs():
    slugs = set()
    for root, dirs, files in os.walk(SOLUTIONS_DIR):
        for f in files:
            if f.endswith('.md'):
                slugs.add(f.replace('.md', ''))
    return slugs

def slugify(text):
    text = text.lower()
    text = re.sub(r'\[bug\]:?\s*', '', text)
    text = re.sub(r'[^a-z0-9\s-]', '', text)
    text = re.sub(r'[\s]+', '-', text.strip())
    text = re.sub(r'-+', '-', text)
    return text[:60].rstrip('-')

def has_chinese_majority(text):
    cn = len(re.findall(r'[\u4e00-\u9fff]', text))
    return cn > len(text) * 0.3 if text else False

def classify(title, body=""):
    text = f"{title} {body[:500]}".lower()
    scores = {}
    for cat, kws in CATEGORY_RULES.items():
        if cat == 'general':
            continue
        scores[cat] = sum(len(kw) for kw in kws if kw in text)
    if not scores or max(scores.values()) == 0:
        return 'general'
    return max(scores, key=scores.get)

def extract_fix(body):
    """Extract fix/workaround from issue body."""
    if not body:
        return None
    patterns = [
        r'(?:##?\s*(?:Fix|Solution|Workaround|Resolution|How to fix))[:\s]*\n([\s\S]{30,800}?)(?:\n#{1,3}\s|\n\n\n|$)',
        r'(?:I fixed|The fix|To fix|Fixed by|Resolved by|The solution|The workaround)[:\s]*([\s\S]{30,800}?)(?:\n\n\n|\n#{1,3}\s|$)',
        r'(?:Workaround|Quick fix)[:\s]*\n([\s\S]{30,800}?)(?:\n#{1,3}\s|\n\n\n|$)',
    ]
    for p in patterns:
        m = re.search(p, body, re.IGNORECASE)
        if m:
            return m.group(1).strip()[:600]
    return None

def extract_symptom(body):
    if not body:
        return ""
    lines = body.split('\n')
    for line in lines:
        line = line.strip()
        if line and not line.startswith('#') and not line.startswith('```') and not line.startswith('|') and len(line) > 30:
            return line[:400]
    return ""

def content_fingerprint(title):
    """Simple fingerprint for dedup."""
    clean = re.sub(r'[^a-z0-9]', '', title.lower())
    return hashlib.md5(clean[:50].encode()).hexdigest()[:12]

def process_github_issues(files, source_label, existing, fingerprints):
    """Process GitHub issue JSON files."""
    results = []
    for fpath in files:
        try:
            with open(fpath) as f:
                issues = json.load(f)
        except:
            continue

        for issue in issues:
            title = issue.get('title', '').strip()
            body = issue.get('body', '') or ''
            url = issue.get('url', '')

            # Skip
            if not title or len(title) < 10:
                continue
            if has_chinese_majority(title):
                continue

            clean_title = re.sub(r'\[Bug\]:?\s*', '', title, flags=re.IGNORECASE).strip()
            if not clean_title:
                continue

            slug = slugify(clean_title)
            if not slug or len(slug) < 5 or slug in existing:
                continue

            fp = content_fingerprint(clean_title)
            if fp in fingerprints:
                continue
            fingerprints.add(fp)

            category = classify(clean_title, body)
            symptom = extract_symptom(body)
            if not symptom:
                symptom = clean_title

            # Try to get embedded fix
            fix = extract_fix(body)
            if fix and len(fix) > 30:
                fix_text = fix
            else:
                fix_text = SOLUTIONS_MAP.get(category, SOLUTIONS_MAP['general'])

            results.append({
                'slug': slug,
                'title': clean_title,
                'category': category,
                'symptom': symptom,
                'fix': fix_text,
                'source': url or source_label,
            })
            existing.add(slug)

    return results

def process_reddit(files, existing, fingerprints):
    """Process Reddit JSON API response files."""
    results = []
    for fpath in files:
        try:
            with open(fpath) as f:
                data = json.load(f)
        except:
            continue

        children = data.get('data', {}).get('children', [])
        for child in children:
            post = child.get('data', {})
            title = post.get('title', '').strip()
            selftext = post.get('selftext', '') or ''
            permalink = post.get('permalink', '')
            score = post.get('score', 0)

            if not title or len(title) < 15 or score < 2:
                continue

            slug = slugify(title)
            if not slug or len(slug) < 5 or slug in existing:
                continue

            fp = content_fingerprint(title)
            if fp in fingerprints:
                continue
            fingerprints.add(fp)

            # Check if it's a real problem post
            text = f"{title} {selftext}".lower()
            problem_words = ['error', 'bug', 'crash', 'fail', 'broken', 'issue', 'problem',
                           'fix', 'help', 'stuck', 'wrong', 'not working', 'can\'t',
                           'expensive', 'cost', 'token', 'hallucin', 'forget', 'memory',
                           'context', 'rate limit', 'timeout', 'slow', 'loop']
            hits = sum(1 for pw in problem_words if pw in text)
            if hits < 2:
                continue

            category = classify(title, selftext)
            symptom = selftext[:400] if selftext else title

            fix = extract_fix(selftext)
            if fix and len(fix) > 30:
                fix_text = fix
            else:
                fix_text = SOLUTIONS_MAP.get(category, SOLUTIONS_MAP['general'])

            source = f"Reddit r/ClaudeAI https://reddit.com{permalink}" if permalink else "Reddit r/ClaudeAI"

            results.append({
                'slug': slug,
                'title': title,
                'category': category,
                'symptom': symptom,
                'fix': fix_text,
                'source': source,
            })
            existing.add(slug)

    return results

def write_solutions(solutions):
    written = 0
    for sol in solutions:
        cat_dir = os.path.join(SOLUTIONS_DIR, sol['category'])
        os.makedirs(cat_dir, exist_ok=True)
        filepath = os.path.join(cat_dir, f"{sol['slug']}.md")
        if os.path.exists(filepath):
            continue

        ct = sol['title'].replace('"', "'")
        content = f"""---
layout: solution
title: "{ct}"
category: {sol['category']}
source: {sol['source'][:80]}
---

# {sol['title']}

## 증상
{sol['symptom']}

## 원인
보고된 버그/문제. 카테고리: {sol['category']}.

## 해결법
{sol['fix']}

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
{sol['source']}
"""
        with open(filepath, 'w') as f:
            f.write(content)
        written += 1
    return written


def main():
    existing = get_existing_slugs()
    fingerprints = set()
    print(f"Existing solutions: {len(existing)}")

    # 1. OpenClaw bugs
    print("\n--- OpenClaw bugs (500) ---")
    oc_bug_files = [os.path.join(CRAWL_DIR, 'oc-bugs.json')]
    oc_bugs = process_github_issues(oc_bug_files, "openclaw/openclaw", existing, fingerprints)
    print(f"  Extracted: {len(oc_bugs)}")

    # 2. OpenClaw keyword searches
    print("\n--- OpenClaw keyword searches ---")
    oc_search_files = glob.glob(os.path.join(CRAWL_DIR, 'oc-s-*.json'))
    oc_search = process_github_issues(oc_search_files, "openclaw/openclaw", existing, fingerprints)
    print(f"  Extracted: {len(oc_search)}")

    # 3. Claude Code bugs
    print("\n--- Claude Code bugs (500) ---")
    cc_bug_files = [os.path.join(CRAWL_DIR, 'cc-bugs.json')]
    cc_bugs = process_github_issues(cc_bug_files, "anthropics/claude-code", existing, fingerprints)
    print(f"  Extracted: {len(cc_bugs)}")

    # 4. Claude Code keyword searches
    print("\n--- Claude Code keyword searches ---")
    cc_search_files = glob.glob(os.path.join(CRAWL_DIR, 'cc-s-*.json'))
    cc_search = process_github_issues(cc_search_files, "anthropics/claude-code", existing, fingerprints)
    print(f"  Extracted: {len(cc_search)}")

    # 5. Reddit
    print("\n--- Reddit r/ClaudeAI ---")
    reddit_files = glob.glob(os.path.join(CRAWL_DIR, 'r-*.json'))
    reddit = process_reddit(reddit_files, existing, fingerprints)
    print(f"  Extracted: {len(reddit)}")

    # Write all
    all_solutions = oc_bugs + oc_search + cc_bugs + cc_search + reddit
    print(f"\n--- Writing {len(all_solutions)} solutions ---")
    written = write_solutions(all_solutions)

    total = len(get_existing_slugs())
    print(f"\nWritten: {written}")
    print(f"Total solutions: {total}")

    print("\nCategory distribution:")
    for cat_dir in sorted(os.listdir(SOLUTIONS_DIR)):
        cat_path = os.path.join(SOLUTIONS_DIR, cat_dir)
        if os.path.isdir(cat_path):
            count = len([f for f in os.listdir(cat_path) if f.endswith('.md')])
            if count > 0:
                print(f"  {cat_dir}: {count}")


if __name__ == "__main__":
    main()
