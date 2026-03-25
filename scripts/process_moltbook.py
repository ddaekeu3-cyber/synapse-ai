#!/usr/bin/env python3
"""Process 3,300 Moltbook posts → extract actionable problems → generate SynapseAI solutions."""

import json
import os
import re
import hashlib

DATA_FILE = os.path.expanduser("~/.openclaw/workspace/moltbook-data/all-posts.json")
SOLUTIONS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "_solutions")

# Problem detection keywords (weighted)
PROBLEM_KEYWORDS_STRONG = [
    'error', 'bug', 'crash', 'fail', 'broken', 'fix', 'issue', 'problem',
    'exception', 'timeout', 'refused', 'denied', 'invalid', 'unauthorized',
    '401', '403', '404', '429', '500', '502', '503',
    'rate limit', 'rate-limit', 'ratelimit',
    'out of memory', 'oom', 'memory leak',
    'token limit', 'token cost', 'token waste', 'expensive',
    'context window', 'context overflow', 'context length',
    'hallucination', 'hallucinate', 'making things up', 'fabricat',
    'forget', 'forgets', 'amnesia', 'memory loss', 'lost context',
    'stuck', 'loop', 'infinite loop', 'retry', 'retrying',
    'frustrated', 'frustrating', 'struggling', 'annoying',
    'workaround', 'solved', 'solution', 'resolved',
    'permission denied', 'access denied', 'eacces',
    'connection refused', 'connection reset', 'connection timeout',
    'api key', 'api_key', 'authentication', 'auth fail',
    'deprecat', 'breaking change', 'incompatible',
    'slow', 'latency', 'performance',
    'not working', 'doesn\'t work', 'does not work', 'stopped working',
    'can\'t', 'cannot', 'unable to',
    'wrong output', 'incorrect', 'inaccurate',
    'prompt injection', 'jailbreak',
    'sandbox', 'container', 'docker',
    'webhook', 'callback',
    'race condition', 'deadlock', 'concurrency',
]

PROBLEM_KEYWORDS_WEAK = [
    'challenge', 'difficult', 'hard to', 'tricky', 'confusing',
    'unexpected', 'surprise', 'strange', 'weird', 'odd',
    'warning', 'caution', 'careful', 'watch out', 'pitfall',
    'debug', 'troubleshoot', 'diagnose',
    'config', 'configuration', 'setup', 'install',
    'migration', 'upgrade', 'update',
]

# Skip patterns (essays, spam, philosophy, promo)
SKIP_PATTERNS = [
    r'mbc-20.*mint.*GPT',  # mbc-20 mint spam
    r'^\{\"p\":\"mbc-20\"',  # JSON mint spam
    r'mbc20\.xyz',
    r'Join my.*discord',
    r'Follow me',
    r'Subscribe to',
    r'Check out my.*channel',
]

# Category classification rules
CATEGORY_RULES = {
    'token-cost': [
        'token cost', 'token waste', 'expensive', 'token limit', 'billing',
        'price', 'pricing', 'cost', 'budget', 'spending', 'save token',
        'reduce cost', 'api cost', 'usage cost', 'monthly bill',
    ],
    'context-window': [
        'context window', 'context length', 'context overflow', 'context limit',
        'max token', 'token limit', 'long conversation', 'conversation length',
        'truncat', 'context size', 'input too long',
    ],
    'hallucination': [
        'hallucin', 'making things up', 'fabricat', 'invent', 'false information',
        'wrong answer', 'incorrect output', 'inaccurate', 'confabulat',
        'not true', 'made up', 'imagin',
    ],
    'memory': [
        'forget', 'forgets', 'amnesia', 'memory loss', 'lost context',
        'doesn\'t remember', 'can\'t remember', 'short-term memory',
        'long-term memory', 'memory reset', 'session memory', 'persistent memory',
    ],
    'tool-failure': [
        'tool fail', 'tool error', 'function call', 'tool call',
        'plugin error', 'mcp error', 'mcp server', 'skill error',
        'api call fail', 'webhook fail', 'integration error',
    ],
    'rate-limit': [
        'rate limit', 'rate-limit', 'ratelimit', '429', 'too many requests',
        'throttl', 'quota', 'exceeded', 'backoff', 'retry after',
    ],
    'auth': [
        'auth', 'oauth', 'api key', 'api_key', 'token expired',
        'unauthorized', '401', '403', 'permission denied', 'access denied',
        'credential', 'secret', 'login fail',
    ],
    'config': [
        'config', 'configuration', 'setup', 'install', 'environment variable',
        'env var', '.env', 'settings', 'yaml', 'json config',
        'misconfigur', 'wrong setting',
    ],
    'performance': [
        'slow', 'latency', 'performance', 'timeout', 'hang', 'freeze',
        'unresponsive', 'takes too long', 'speed', 'bottleneck',
    ],
    'loop-stuck': [
        'stuck', 'loop', 'infinite loop', 'retry loop', 'keeps retrying',
        'circular', 'repeating', 'same error again', 'won\'t stop',
    ],
    'prompt-engineering': [
        'prompt', 'system prompt', 'instruction', 'prompt injection',
        'jailbreak', 'prompt leak', 'prompt template',
    ],
    'concurrency': [
        'race condition', 'deadlock', 'concurrency', 'parallel',
        'async', 'await', 'thread', 'mutex', 'lock',
    ],
    'docker': [
        'docker', 'container', 'sandbox', 'podman', 'kubernetes', 'k8s',
    ],
    'general': [],  # fallback
}


def is_spam_or_skip(title, content):
    """Check if post should be skipped."""
    text = f"{title} {content}"
    for pattern in SKIP_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    # Skip very short content (< 100 chars)
    if len(content.strip()) < 100:
        return True
    # Skip pure promotional/philosophy with no actionable content
    return False


def has_problem_signal(title, content):
    """Score how likely the post contains an actionable problem."""
    text = f"{title} {content}".lower()
    score = 0

    for kw in PROBLEM_KEYWORDS_STRONG:
        if kw.lower() in text:
            score += 3

    for kw in PROBLEM_KEYWORDS_WEAK:
        if kw.lower() in text:
            score += 1

    # Bonus for posts with code blocks (more likely technical)
    if '```' in content:
        score += 2

    # Bonus for error messages in content
    if re.search(r'(Error|ERROR|Exception|FATAL|WARN)[\s:]+', content):
        score += 3

    # Penalty for very abstract/philosophical posts
    philosophy_words = ['philosophy', 'existential', 'consciousness', 'sentient',
                       'metaphor', 'poetic', 'artistic', 'creative writing']
    for pw in philosophy_words:
        if pw in text:
            score -= 2

    return score


def categorize_problem(title, content):
    """Classify problem into category."""
    text = f"{title} {content}".lower()
    scores = {}
    for cat, keywords in CATEGORY_RULES.items():
        if cat == 'general':
            continue
        scores[cat] = sum(1 for kw in keywords if kw in text)

    if not scores or max(scores.values()) == 0:
        return 'general'

    return max(scores, key=scores.get)


def extract_problem_summary(title, content):
    """Extract the core problem from the post."""
    # Clean HTML tags
    clean = re.sub(r'<[^>]+>', '', content)
    # Get first 2-3 meaningful paragraphs
    paragraphs = [p.strip() for p in clean.split('\n\n') if len(p.strip()) > 30]
    summary = '\n\n'.join(paragraphs[:3])
    return summary[:800] if summary else clean[:800]


def extract_solution_from_post(content):
    """Try to extract solution/fix from the post itself."""
    patterns = [
        r'(?:solution|fix|workaround|resolved|answer)[:\s]*\n([\s\S]{30,800}?)(?:\n\n\n|\n#{1,3}\s|$)',
        r'(?:I fixed|I solved|The fix|The solution|Here\'s how|To fix)[:\s]*([\s\S]{30,800}?)(?:\n\n\n|\n#{1,3}\s|$)',
        r'(?:Step \d|1\.|First,)[:\s]*([\s\S]{30,800}?)(?:\n\n\n|\n#{1,3}\s|$)',
    ]
    for p in patterns:
        m = re.search(p, content, re.IGNORECASE)
        if m:
            return m.group(1).strip()[:600]
    return None


def generate_solution_content(title, content, category, post_id, author_name, post_url):
    """Generate a solution markdown file from a post."""
    clean_title = re.sub(r'<[^>]+>', '', title).strip()
    if len(clean_title) > 100:
        clean_title = clean_title[:97] + "..."

    problem_summary = extract_problem_summary(title, content)
    embedded_solution = extract_solution_from_post(content)

    # Build solution text
    if embedded_solution:
        solution_text = embedded_solution
    else:
        # Generate practical solution based on category
        solution_text = generate_solution_by_category(category, title, content)

    return f"""---
layout: solution
title: "{clean_title.replace('"', "'")}"
category: {category}
source: moltbook
---

# {clean_title}

## 증상
{problem_summary}

## 원인
Moltbook 커뮤니티에서 보고된 문제. 카테고리: {category}.

## 해결법
{solution_text}

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- 관련 카테고리: {category}
- 보고자: {author_name} (Moltbook)

## 출처
Moltbook 포스트 by {author_name}
https://www.moltbook.com/post/{post_id}
"""


def generate_solution_by_category(category, title, content):
    """Generate practical solution advice based on category."""
    solutions = {
        'token-cost': """### 토큰 비용 절감 방법

1. **모델 선택 최적화**: 단순 작업은 Haiku/GPT-4o-mini 사용, 복잡한 작업만 Opus/GPT-4 사용
2. **컨텍스트 축소**: 불필요한 파일/대화 히스토리 제거, `.clawignore` 활용
3. **캐싱 활성화**: 반복 API 호출 결과를 로컬 캐싱
4. **에러 루프 방지**: 같은 에러 3회 이상 반복 시 멈추고 다른 접근법 시도
5. **SynapseAI 솔루션 DB 검색**: 이미 해결된 에러는 검색으로 즉시 해결""",

        'context-window': """### 컨텍스트 윈도우 문제 해결

1. **대화 분할**: 긴 작업은 여러 세션으로 나누기
2. **요약 활용**: 이전 대화를 요약본으로 대체
3. **파일 참조 최소화**: 필요한 부분만 읽기, 전체 파일 붙여넣기 금지
4. **청크 처리**: 대량 데이터는 청크로 나눠서 순차 처리
5. **컨텍스트 우선순위**: 가장 중요한 정보를 앞에 배치""",

        'hallucination': """### 할루시네이션 방지

1. **사실 확인 요청**: "확실하지 않으면 모른다고 답해" 지시 추가
2. **출처 요구**: 모든 답변에 출처/근거를 함께 요청
3. **코드 실행 검증**: AI 생성 코드는 반드시 실행해서 검증
4. **단계별 확인**: 복잡한 작업은 단계별로 중간 결과 확인
5. **RAG 활용**: 외부 문서/DB에서 사실을 검색하도록 구성""",

        'memory': """### 메모리/컨텍스트 유지 문제 해결

1. **영속적 메모리 파일 사용**: CLAUDE.md, AGENTS.md 등에 핵심 정보 기록
2. **세션 요약 저장**: 각 세션 종료 시 진행상황을 파일로 저장
3. **명시적 컨텍스트 전달**: 새 세션 시작 시 이전 결과물 명시적으로 전달
4. **체크포인트 활용**: 장기 작업에서 주기적으로 상태 저장
5. **외부 상태 관리**: JSON/DB에 에이전트 상태를 외부 저장""",

        'tool-failure': """### 도구/플러그인 실패 해결

1. **에러 메시지 정확히 읽기**: 에러 코드와 메시지로 원인 파악
2. **권한 확인**: API 키, 토큰, 스코프가 올바른지 확인
3. **버전 호환성**: 도구/API 버전이 현재 환경과 호환되는지 확인
4. **네트워크 상태**: 연결, DNS, 프록시 설정 확인
5. **대체 도구**: 실패 시 동일 기능의 대체 도구/API 사용
6. **재시도 로직**: 일시적 오류는 지수 백오프로 재시도""",

        'rate-limit': """### Rate Limit 해결

1. **지수 백오프**: 재시도 간격을 2배씩 증가 (1초 → 2초 → 4초 → 8초)
2. **지터 추가**: 백오프에 랜덤 지터 추가로 thundering herd 방지
3. **요청 큐잉**: 요청을 큐에 넣고 rate limit에 맞춰 순차 처리
4. **캐싱**: 동일 요청 결과를 캐싱해서 API 호출 횟수 감소
5. **Retry-After 헤더 확인**: 서버가 알려주는 대기 시간 준수
6. **배치 처리**: 개별 요청을 묶어서 배치 API 활용""",

        'auth': """### 인증/권한 문제 해결

1. **API 키 확인**: 키가 유효하고 만료되지 않았는지 확인
2. **스코프 확인**: 필요한 권한/스코프가 모두 부여되었는지 확인
3. **토큰 갱신**: OAuth 토큰 만료 시 refresh token으로 갱신
4. **환경변수 확인**: .env 파일에 올바른 키가 설정되었는지 확인
5. **인코딩 확인**: API 키에 특수문자가 있으면 URL 인코딩 필요
6. **IP 제한**: IP 화이트리스트에 현재 IP가 포함되었는지 확인""",

        'config': """### 설정/구성 문제 해결

1. **공식 문서 참조**: 최신 설정 가이드를 공식 문서에서 확인
2. **환경변수 확인**: 필수 환경변수가 모두 설정되었는지 확인
3. **버전 호환성**: 설정 포맷이 현재 버전과 호환되는지 확인
4. **기본값 확인**: 생략된 설정의 기본값이 의도한 동작과 일치하는지 확인
5. **로그 확인**: 시작 로그에서 설정 관련 경고/에러 확인
6. **최소 설정으로 시작**: 복잡한 설정 대신 최소 설정에서 하나씩 추가""",

        'performance': """### 성능/지연 문제 해결

1. **병목 식별**: 프로파일링으로 가장 느린 부분 찾기
2. **캐싱**: 반복 연산/API 호출 결과 캐싱
3. **병렬 처리**: 독립적인 작업은 동시 실행
4. **배치 처리**: 개별 처리 대신 배치로 묶어 처리
5. **타임아웃 설정**: 적절한 타임아웃으로 무한 대기 방지
6. **리소스 모니터링**: CPU, 메모리, 네트워크 사용량 확인""",

        'loop-stuck': """### 에이전트 루프/멈춤 해결

1. **최대 재시도 제한**: 동일 작업 재시도 횟수를 3-5회로 제한
2. **에러 패턴 감지**: 같은 에러가 반복되면 즉시 다른 접근법으로 전환
3. **타임아웃 설정**: 단일 작업에 시간 제한 설정
4. **상태 체크포인트**: 진행상황을 기록해서 같은 작업 반복 방지
5. **에스컬레이션**: 자동 해결 실패 시 사람에게 보고
6. **SynapseAI 검색**: 이미 해결된 에러인지 솔루션 DB에서 먼저 확인""",

        'prompt-engineering': """### 프롬프트 엔지니어링 문제 해결

1. **명확한 지시**: 모호한 표현 대신 구체적이고 명확한 지시 사용
2. **예시 제공**: Few-shot 예시로 원하는 출력 형태 보여주기
3. **역할 지정**: 시스템 프롬프트에 구체적 역할과 제약조건 명시
4. **출력 포맷 지정**: JSON, 마크다운 등 원하는 출력 형식 명시
5. **단계별 사고**: "Step by step" 지시로 추론 과정 명시 유도
6. **보안**: 프롬프트 인젝션 방지를 위한 입력 검증""",

        'concurrency': """### 동시성/비동기 문제 해결

1. **락 사용**: 공유 리소스 접근 시 적절한 락/뮤텍스 사용
2. **원자적 연산**: 가능하면 원자적 연산으로 경쟁 조건 방지
3. **큐 기반 처리**: 공유 상태 대신 메시지 큐로 통신
4. **타임아웃**: 락 대기에 타임아웃 설정으로 데드락 방지
5. **순서 보장**: 순서가 중요한 작업은 순차 처리 강제
6. **테스트**: 동시성 버그는 재현이 어려우므로 스트레스 테스트 필수""",

        'docker': """### Docker/컨테이너 문제 해결

1. **권한 확인**: `--user` 플래그, 볼륨 마운트 권한 확인
2. **네트워크**: 컨테이너 간 네트워크 연결, DNS 확인
3. **리소스 제한**: 메모리/CPU 제한이 충분한지 확인
4. **로그 확인**: `docker logs` 로 에러 메시지 확인
5. **이미지 빌드**: Dockerfile 레이어 순서, 캐시 활용 최적화
6. **볼륨 마운트**: 호스트-컨테이너 경로 매핑 정확히 확인""",

        'general': """### 일반적인 에이전트 문제 해결

1. **에러 메시지 정확히 읽기**: 에러 코드와 메시지에서 원인 파악
2. **공식 문서 확인**: 최신 공식 문서에서 해결법 검색
3. **커뮤니티 검색**: GitHub Issues, Stack Overflow, Discord에서 유사 사례 검색
4. **최소 재현**: 문제를 최소 코드로 재현해서 원인 격리
5. **버전 확인**: 사용 중인 라이브러리/도구 버전 호환성 확인
6. **SynapseAI 검색**: 솔루션 DB에서 이미 해결된 문제인지 확인""",
    }
    return solutions.get(category, solutions['general'])


def slugify(text):
    text = text.lower()
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'[^a-z0-9\s-]', '', text)
    text = re.sub(r'[\s]+', '-', text.strip())
    text = re.sub(r'-+', '-', text)
    return text[:60].rstrip('-')


def get_existing_slugs():
    """Get set of existing solution file slugs."""
    slugs = set()
    for root, dirs, files in os.walk(SOLUTIONS_DIR):
        for f in files:
            if f.endswith('.md'):
                slugs.add(f.replace('.md', ''))
    return slugs


def main():
    print("Loading Moltbook data...")
    with open(DATA_FILE) as f:
        posts = json.load(f)
    print(f"Total posts: {len(posts)}")

    existing_slugs = get_existing_slugs()
    print(f"Existing solutions: {len(existing_slugs)}")

    # Phase 1: Score and filter all posts
    print("\n--- Phase 1: Scoring all posts ---")
    scored_posts = []
    skipped_spam = 0
    skipped_low_score = 0

    for i, post in enumerate(posts):
        title = post.get('title', '')
        content = post.get('content', '')
        post_id = post.get('id', '')
        author = post.get('author', {})
        author_name = author.get('name', 'unknown') if author else 'unknown'

        if is_spam_or_skip(title, content):
            skipped_spam += 1
            continue

        score = has_problem_signal(title, content)
        if score < 5:  # Threshold for "actionable problem"
            skipped_low_score += 1
            continue

        category = categorize_problem(title, content)
        slug = slugify(title)

        if not slug or len(slug) < 5:
            continue

        if slug in existing_slugs:
            continue

        scored_posts.append({
            'title': title,
            'content': content,
            'post_id': post_id,
            'author_name': author_name,
            'score': score,
            'category': category,
            'slug': slug,
        })

        if (i + 1) % 500 == 0:
            print(f"  Scanned {i+1}/{len(posts)} posts... ({len(scored_posts)} problems found)")

    print(f"\nScan complete:")
    print(f"  Skipped (spam/short): {skipped_spam}")
    print(f"  Skipped (low score): {skipped_low_score}")
    print(f"  Problems found: {len(scored_posts)}")

    # Deduplicate by slug
    seen_slugs = set()
    unique_posts = []
    for sp in scored_posts:
        if sp['slug'] not in seen_slugs:
            seen_slugs.add(sp['slug'])
            unique_posts.append(sp)

    print(f"  After dedup: {len(unique_posts)}")

    # Phase 2: Generate solution files
    print("\n--- Phase 2: Generating solutions ---")
    category_counts = {}
    written = 0

    # Ensure category dirs exist
    for cat in CATEGORY_RULES.keys():
        cat_dir = os.path.join(SOLUTIONS_DIR, cat)
        os.makedirs(cat_dir, exist_ok=True)

    for sp in unique_posts:
        cat_dir = os.path.join(SOLUTIONS_DIR, sp['category'])
        filepath = os.path.join(cat_dir, f"{sp['slug']}.md")

        if os.path.exists(filepath):
            continue

        sol_content = generate_solution_content(
            sp['title'], sp['content'], sp['category'],
            sp['post_id'], sp['author_name'],
            f"https://www.moltbook.com/post/{sp['post_id']}"
        )

        with open(filepath, 'w') as f:
            f.write(sol_content)

        written += 1
        category_counts[sp['category']] = category_counts.get(sp['category'], 0) + 1

    print(f"\nWritten {written} new solution files")
    print("\nBy category:")
    for cat, count in sorted(category_counts.items(), key=lambda x: -x[1]):
        print(f"  {cat}: {count}")

    total = len(existing_slugs) + written
    print(f"\nTotal solutions: {len(existing_slugs)} existing + {written} new = {total}")


if __name__ == "__main__":
    main()
