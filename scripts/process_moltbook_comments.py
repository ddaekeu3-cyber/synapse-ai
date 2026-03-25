#!/usr/bin/env python3
"""Process 2,721 Moltbook comments → extract actionable problems → generate SynapseAI solutions."""

import json
import os
import re

DATA_FILE = os.path.expanduser("~/.openclaw/workspace/moltbook-data/all-comments.json")
SOLUTIONS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "_solutions")

PROBLEM_KEYWORDS_STRONG = [
    'error', 'bug', 'crash', 'fail', 'broken', 'fix', 'issue', 'problem',
    'exception', 'timeout', 'refused', 'denied', 'invalid', 'unauthorized',
    '401', '403', '404', '429', '500', '502', '503',
    'rate limit', 'rate-limit', 'ratelimit',
    'out of memory', 'oom', 'memory leak',
    'token limit', 'token cost', 'token waste', 'expensive',
    'context window', 'context overflow', 'context length',
    'hallucination', 'hallucinate', 'making things up',
    'forget', 'forgets', 'amnesia', 'memory loss', 'lost context',
    'stuck', 'loop', 'infinite loop', 'retry',
    'frustrated', 'frustrating', 'struggling',
    'workaround', 'solved', 'solution', 'resolved',
    'permission denied', 'access denied',
    'connection refused', 'connection reset', 'connection timeout',
    'api key', 'authentication', 'auth fail',
    'deprecat', 'breaking change', 'incompatible',
    'slow', 'latency', 'performance',
    'not working', 'doesn\'t work', 'stopped working',
    'can\'t', 'cannot', 'unable to',
    'wrong output', 'incorrect', 'inaccurate',
    'sandbox', 'container', 'docker',
    'race condition', 'deadlock', 'concurrency',
]

PROBLEM_KEYWORDS_WEAK = [
    'challenge', 'difficult', 'hard to', 'tricky', 'confusing',
    'unexpected', 'strange', 'weird',
    'warning', 'careful', 'watch out', 'pitfall',
    'debug', 'troubleshoot', 'diagnose',
    'config', 'setup', 'install',
]

SKIP_PATTERNS = [
    r'mbc-20.*mint',
    r'mbc20\.xyz',
    r'^upvoted',
    r'^solid contribution',
    r'^great post',
    r'^nice',
    r'^thanks',
    r'^interesting',
    r'^good point',
    r'^i agree',
    r'^well said',
    r'^\+1',
    r'^🦞',
]

CATEGORY_RULES = {
    'token-cost': ['token cost', 'token waste', 'expensive', 'token limit', 'billing', 'price', 'cost', 'budget', 'save token', 'reduce cost', 'api cost'],
    'context-window': ['context window', 'context length', 'context overflow', 'context limit', 'max token', 'token limit', 'long conversation', 'truncat'],
    'hallucination': ['hallucin', 'making things up', 'fabricat', 'wrong answer', 'incorrect output', 'inaccurate', 'not true', 'made up'],
    'memory': ['forget', 'forgets', 'amnesia', 'memory loss', 'lost context', 'doesn\'t remember', 'memory reset', 'persistent memory'],
    'tool-failure': ['tool fail', 'tool error', 'function call', 'tool call', 'plugin error', 'mcp error', 'skill error', 'api call fail'],
    'rate-limit': ['rate limit', 'rate-limit', '429', 'too many requests', 'throttl', 'quota', 'exceeded', 'backoff'],
    'auth': ['auth', 'oauth', 'api key', 'token expired', 'unauthorized', '401', '403', 'permission denied', 'credential'],
    'config': ['config', 'setup', 'install', 'environment variable', 'env var', '.env', 'settings', 'misconfigur'],
    'performance': ['slow', 'latency', 'performance', 'timeout', 'hang', 'freeze', 'unresponsive', 'speed', 'bottleneck'],
    'loop-stuck': ['stuck', 'loop', 'infinite loop', 'retry loop', 'keeps retrying', 'repeating', 'same error again'],
    'prompt-engineering': ['prompt', 'system prompt', 'instruction', 'prompt injection', 'jailbreak', 'prompt template'],
    'concurrency': ['race condition', 'deadlock', 'concurrency', 'parallel', 'async', 'thread', 'mutex'],
    'docker': ['docker', 'container', 'sandbox', 'podman', 'kubernetes'],
    'general': [],
}

CATEGORY_SOLUTIONS = {
    'token-cost': """### 토큰 비용 절감
1. **모델 선택 최적화**: 단순 작업은 저비용 모델 사용
2. **컨텍스트 축소**: 불필요한 파일/히스토리 제거
3. **캐싱**: 반복 API 호출 결과 캐싱
4. **에러 루프 방지**: 같은 에러 3회 반복 시 중단
5. **SynapseAI 검색**: 이미 해결된 에러는 DB에서 즉시 해결""",
    'context-window': """### 컨텍스트 윈도우 관리
1. **대화 분할**: 긴 작업은 여러 세션으로 나누기
2. **요약 활용**: 이전 대화를 요약본으로 대체
3. **파일 참조 최소화**: 필요한 부분만 읽기
4. **청크 처리**: 대량 데이터는 나눠서 처리""",
    'hallucination': """### 할루시네이션 방지
1. **사실 확인 요청**: "확실하지 않으면 모른다고 답해"
2. **출처 요구**: 답변에 근거를 함께 요청
3. **코드 실행 검증**: 생성 코드는 반드시 실행 확인
4. **RAG 활용**: 외부 문서에서 사실 검색""",
    'memory': """### 메모리 유지
1. **영속적 파일 사용**: CLAUDE.md 등에 핵심 정보 기록
2. **세션 요약 저장**: 종료 시 진행상황 파일로 저장
3. **명시적 컨텍스트 전달**: 새 세션에 이전 결과 전달
4. **외부 상태 관리**: JSON/DB에 에이전트 상태 저장""",
    'tool-failure': """### 도구 실패 해결
1. **에러 메시지 정확히 읽기**: 에러 코드로 원인 파악
2. **권한 확인**: API 키, 토큰, 스코프 확인
3. **버전 호환성**: 도구/API 버전 호환 확인
4. **대체 도구**: 실패 시 대체 도구/API 사용""",
    'rate-limit': """### Rate Limit 해결
1. **지수 백오프**: 재시도 간격 2배씩 증가
2. **지터 추가**: 랜덤 지터로 thundering herd 방지
3. **캐싱**: 동일 요청 캐싱으로 호출 횟수 감소
4. **Retry-After 헤더 확인**: 서버 지시 대기 시간 준수""",
    'auth': """### 인증 문제 해결
1. **API 키 확인**: 유효성, 만료 여부 확인
2. **스코프 확인**: 필요 권한 부여 확인
3. **토큰 갱신**: refresh token으로 갱신
4. **환경변수 확인**: .env 설정 확인""",
    'config': """### 설정 문제 해결
1. **공식 문서 참조**: 최신 가이드 확인
2. **환경변수 확인**: 필수 변수 설정 확인
3. **버전 호환성**: 설정 포맷 호환 확인
4. **최소 설정으로 시작**: 하나씩 추가하며 확인""",
    'performance': """### 성능 개선
1. **병목 식별**: 프로파일링으로 느린 부분 찾기
2. **캐싱**: 반복 연산/호출 캐싱
3. **병렬 처리**: 독립 작업 동시 실행
4. **타임아웃 설정**: 무한 대기 방지""",
    'loop-stuck': """### 루프/멈춤 해결
1. **최대 재시도 제한**: 3-5회로 제한
2. **에러 패턴 감지**: 반복 에러 시 다른 접근법 전환
3. **타임아웃 설정**: 단일 작업 시간 제한
4. **에스컬레이션**: 실패 시 사람에게 보고""",
    'prompt-engineering': """### 프롬프트 개선
1. **명확한 지시**: 구체적이고 명확한 표현
2. **예시 제공**: Few-shot으로 원하는 출력 보여주기
3. **역할 지정**: 구체적 역할과 제약조건 명시
4. **출력 포맷 지정**: JSON, 마크다운 등 형식 명시""",
    'concurrency': """### 동시성 문제 해결
1. **락 사용**: 공유 리소스에 적절한 락 사용
2. **원자적 연산**: 경쟁 조건 방지
3. **큐 기반 처리**: 메시지 큐로 통신
4. **타임아웃**: 데드락 방지""",
    'docker': """### Docker 문제 해결
1. **권한 확인**: --user 플래그, 볼륨 권한 확인
2. **네트워크**: 컨테이너 간 연결 확인
3. **로그 확인**: docker logs로 에러 확인
4. **볼륨 마운트**: 경로 매핑 확인""",
    'general': """### 일반 해결법
1. **에러 메시지 읽기**: 에러 코드에서 원인 파악
2. **공식 문서 확인**: 최신 문서에서 해결법 검색
3. **커뮤니티 검색**: GitHub Issues, Discord에서 유사 사례 검색
4. **SynapseAI 검색**: 솔루션 DB에서 기존 해결법 확인""",
}


def is_skip(content):
    text = content.strip().lower()
    for p in SKIP_PATTERNS:
        if re.search(p, text, re.IGNORECASE):
            return True
    if len(content.strip()) < 80:
        return True
    return False


def score_comment(content):
    text = content.lower()
    score = 0
    for kw in PROBLEM_KEYWORDS_STRONG:
        if kw in text:
            score += 3
    for kw in PROBLEM_KEYWORDS_WEAK:
        if kw in text:
            score += 1
    if '```' in content:
        score += 2
    if re.search(r'(Error|ERROR|Exception|FATAL)[\s:]+', content):
        score += 3
    philosophy = ['philosophy', 'existential', 'consciousness', 'sentient', 'poetic']
    for pw in philosophy:
        if pw in text:
            score -= 2
    return score


def categorize(content):
    text = content.lower()
    scores = {}
    for cat, kws in CATEGORY_RULES.items():
        if cat == 'general':
            continue
        scores[cat] = sum(1 for kw in kws if kw in text)
    if not scores or max(scores.values()) == 0:
        return 'general'
    return max(scores, key=scores.get)


def slugify(text):
    text = text.lower()
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'[^a-z0-9\s-]', '', text)
    text = re.sub(r'[\s]+', '-', text.strip())
    text = re.sub(r'-+', '-', text)
    return text[:60].rstrip('-')


def make_title(content):
    """Create a title from comment content."""
    clean = re.sub(r'<[^>]+>', '', content)
    clean = re.sub(r'```[\s\S]*?```', '', clean)
    clean = re.sub(r'\n+', ' ', clean).strip()
    # First sentence or first 80 chars
    m = re.match(r'^(.{20,80}?[.!?])\s', clean)
    if m:
        return m.group(1)
    return clean[:80] + ('...' if len(clean) > 80 else '')


def get_existing_slugs():
    slugs = set()
    for root, dirs, files in os.walk(SOLUTIONS_DIR):
        for f in files:
            if f.endswith('.md'):
                slugs.add(f.replace('.md', ''))
    return slugs


def process_replies(replies, results, existing_slugs, seen_slugs):
    """Recursively process nested replies."""
    if not replies:
        return
    for reply in replies:
        content = reply.get('content', '')
        author = reply.get('author', {})
        author_name = author.get('name', 'unknown') if author else 'unknown'

        if is_skip(content):
            continue

        score = score_comment(content)
        if score < 4:
            continue

        title = make_title(content)
        slug = slugify(title)
        if not slug or len(slug) < 5 or slug in existing_slugs or slug in seen_slugs:
            continue

        seen_slugs.add(slug)
        category = categorize(content)
        results.append({
            'title': title,
            'content': content,
            'comment_id': reply.get('id', ''),
            'post_id': reply.get('post_id', ''),
            'author_name': author_name,
            'score': score,
            'category': category,
            'slug': slug,
        })

        # Process nested replies
        if reply.get('replies'):
            process_replies(reply['replies'], results, existing_slugs, seen_slugs)


def main():
    print("Loading Moltbook comments...")
    with open(DATA_FILE) as f:
        comments = json.load(f)
    print(f"Total comments: {len(comments)}")

    existing_slugs = get_existing_slugs()
    print(f"Existing solutions: {len(existing_slugs)}")

    # Phase 1: Score and filter
    print("\n--- Phase 1: Scoring comments ---")
    results = []
    seen_slugs = set()
    skipped = 0

    for i, comment in enumerate(comments):
        content = comment.get('content', '')
        author = comment.get('author', {})
        author_name = author.get('name', 'unknown') if author else 'unknown'
        post_id = comment.get('post_id', '')

        if is_skip(content):
            skipped += 1
            continue

        score = score_comment(content)
        if score < 4:
            skipped += 1
            continue

        title = make_title(content)
        slug = slugify(title)
        if not slug or len(slug) < 5 or slug in existing_slugs or slug in seen_slugs:
            continue

        seen_slugs.add(slug)
        category = categorize(content)
        results.append({
            'title': title,
            'content': content,
            'comment_id': comment.get('id', ''),
            'post_id': post_id,
            'author_name': author_name,
            'score': score,
            'category': category,
            'slug': slug,
        })

        # Process replies
        if comment.get('replies'):
            process_replies(comment['replies'], results, existing_slugs, seen_slugs)

        if (i + 1) % 500 == 0:
            print(f"  Scanned {i+1}/{len(comments)}... ({len(results)} problems found)")

    print(f"\nScan complete:")
    print(f"  Skipped: {skipped}")
    print(f"  Problems found: {len(results)}")

    # Phase 2: Generate solution files
    print("\n--- Phase 2: Generating solutions ---")
    cat_counts = {}
    written = 0

    for cat in CATEGORY_RULES:
        os.makedirs(os.path.join(SOLUTIONS_DIR, cat), exist_ok=True)

    for r in results:
        cat_dir = os.path.join(SOLUTIONS_DIR, r['category'])
        filepath = os.path.join(cat_dir, f"{r['slug']}.md")
        if os.path.exists(filepath):
            continue

        clean_title = r['title'].replace('"', "'")
        problem_text = re.sub(r'<[^>]+>', '', r['content'])[:800]
        solution_text = CATEGORY_SOLUTIONS.get(r['category'], CATEGORY_SOLUTIONS['general'])

        content = f"""---
layout: solution
title: "{clean_title}"
category: {r['category']}
source: moltbook-comment
---

# {clean_title}

## 증상
{problem_text}

## 원인
Moltbook 커뮤니티 댓글에서 보고된 문제. 카테고리: {r['category']}.

## 해결법
{solution_text}

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- 관련 카테고리: {r['category']}
- 보고자: {r['author_name']} (Moltbook)

## 출처
Moltbook 댓글 by {r['author_name']}
https://www.moltbook.com/post/{r['post_id']}
"""
        with open(filepath, 'w') as f:
            f.write(content)
        written += 1
        cat_counts[r['category']] = cat_counts.get(r['category'], 0) + 1

    print(f"\nWritten {written} new solution files")
    print("\nBy category:")
    for cat, count in sorted(cat_counts.items(), key=lambda x: -x[1]):
        print(f"  {cat}: {count}")

    total = len(existing_slugs) + written
    print(f"\nTotal solutions: {len(existing_slugs)} existing + {written} new = {total}")


if __name__ == "__main__":
    main()
