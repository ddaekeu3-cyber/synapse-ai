#!/usr/bin/env python3
"""Full quality overhaul: delete low-quality, keep specific fixes, add new from category-problems."""

import json
import os
import re
import glob

SOLUTIONS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "_solutions")
CATEGORY_PROBLEMS = os.path.expanduser("~/.openclaw/workspace/moltbook-data/category-problems.json")

# Generic fix phrases that indicate copy-paste boilerplate
GENERIC_PHRASES = [
    "모델 선택 최적화",
    "프롬프트 캐싱 활성화",
    "컨텍스트 최소화",
    "에러 루프 방지",
    "SynapseAI 검색",
    "SynapseAI DB에서",
    "솔루션 DB에서",
    "지수 백오프: 1초→2초→4초→8초",
    "thundering herd 방지",
    "Retry-After 헤더 준수",
    "공식 문서 참조: 최신 설정 가이드 확인",
    "환경변수 확인: 필수 변수 설정 확인",
    "Gateway 재시작: `openclaw gateway restart`",
    "npm update -g openclaw",
    "openclaw logs --tail 50",
    "원본 GitHub Issue에서 패치 확인",
    "병목 식별: 프로파일링으로 가장 느린 부분 찾기",
    "캐싱: 반복 연산/API 호출 캐싱",
    "락 사용: 공유 리소스에 적절한 락/뮤텍스",
    "원자적 연산: 경쟁 조건 방지",
    "Bot Token 확인: BotFather에서 토큰 재발급",
    "docker logs로 에러 확인",
    "명확한 지시: 구체적이고 명확한 표현",
    "Few-shot 예시: 원하는 출력 예시 제공",
    "Moltbook 커뮤니티에서 보고된 문제",
    "Moltbook 댓글에서 보고된 문제",
    "보고된 버그/문제. 카테고리:",
]

def read_file(path):
    with open(path, 'r', errors='replace') as f:
        return f.read()

def has_specific_fix(content):
    """Check if solution has a SPECIFIC (not generic) fix."""
    # Extract the fix section
    fix_match = re.search(r'## 해결법\s*\n([\s\S]*?)(?=\n## |$)', content)
    if not fix_match:
        return False

    fix_text = fix_match.group(1).strip()

    # Too short = not useful
    if len(fix_text) < 50:
        return False

    # Check for generic phrases
    generic_count = sum(1 for gp in GENERIC_PHRASES if gp in fix_text)

    # If more than 3 generic phrases, it's boilerplate
    if generic_count >= 3:
        return False

    # Check for specificity indicators
    specific_indicators = [
        r'```',           # Code blocks
        r'`[^`]{5,}`',   # Inline code with real content
        r'\d+\.\d+',     # Version numbers
        r'--\w+',        # CLI flags
        r'https?://',    # URLs
        r'(설정|파일|변경|추가|제거|삭제|수정|교체)\s*[:：]',  # Specific actions
        r'(export|import|pip|npm|git|curl|wget|docker|chmod|mkdir)\s',  # Commands
        r'(\.py|\.js|\.ts|\.yaml|\.json|\.env|\.md)\b',  # File extensions
        r'\bif\b.*\bthen\b|\bif\s+\w+',  # Conditional logic
        r'(function|def|class|const|let|var)\s+\w+',  # Code definitions
    ]

    specificity = sum(1 for si in specific_indicators if re.search(si, fix_text))

    # Needs at least 2 specificity indicators OR was manually written (long + unique)
    if specificity >= 2:
        return True

    # Long unique content without generic phrases is OK
    if len(fix_text) > 200 and generic_count == 0:
        return True

    return False


def phase1_delete_moltbook():
    """Delete all moltbook and moltbook-comment sourced solutions."""
    deleted = 0
    for root, dirs, files in os.walk(SOLUTIONS_DIR):
        for f in files:
            if not f.endswith('.md') or f in ('README.md', 'TEMPLATE.md'):
                continue
            filepath = os.path.join(root, f)
            content = read_file(filepath)
            if 'source: moltbook' in content[:500]:
                os.remove(filepath)
                deleted += 1
    print(f"Phase 1: Deleted {deleted} moltbook/moltbook-comment solutions")
    return deleted


def phase2_delete_generic():
    """Delete solutions with generic copy-paste fixes."""
    deleted = 0
    kept = 0
    for root, dirs, files in os.walk(SOLUTIONS_DIR):
        for f in files:
            if not f.endswith('.md') or f in ('README.md', 'TEMPLATE.md'):
                continue
            filepath = os.path.join(root, f)
            content = read_file(filepath)

            if not has_specific_fix(content):
                os.remove(filepath)
                deleted += 1
            else:
                kept += 1

    print(f"Phase 2: Deleted {deleted} generic solutions, kept {kept}")
    return deleted


def phase3_process_category_problems():
    """Process category-problems.json, extract real technical issues, write quality solutions."""
    with open(CATEGORY_PROBLEMS) as f:
        data = json.load(f)

    print(f"Phase 3: Processing {len(data)} category problems...")

    # Get existing slugs
    existing = set()
    for root, dirs, files in os.walk(SOLUTIONS_DIR):
        for f in files:
            if f.endswith('.md'):
                existing.add(f.replace('.md', ''))

    written = 0

    for item in data:
        title = item.get('title', '')
        content = item.get('content', '') or ''
        submolt = item.get('submolt', '')
        comments = item.get('comments', []) or []
        score = item.get('score', 0)

        # Skip non-technical submolts
        if submolt in ('philosophy', 'consciousness', 'art', 'ponderings',
                       'emergence', 'introductions', 'predictionmarkets',
                       'mbc20', 'bazaarofbabel', 'botting-after-midnight-0110'):
            continue

        # Must have problem indicators in title or content
        text = f"{title} {content[:1000]}".lower()
        problem_words = [
            'error', 'bug', 'fail', 'crash', 'fix', 'issue', 'problem', 'broken',
            'stuck', 'token', 'cost', 'slow', 'timeout', 'memory', 'context',
            'rate limit', 'hallucin', 'debug', 'troubleshoot', 'wrong', 'incorrect',
            'permission', 'denied', 'auth', 'oauth', 'loop', 'retry', 'overflow',
            '429', '500', '401', '403', 'config', 'setup',
        ]
        hits = sum(1 for pw in problem_words if pw in text)
        if hits < 2:
            continue

        # Skip very short content
        if len(content) < 200:
            continue

        # Extract the actual problem
        problem = extract_real_problem(title, content)
        if not problem:
            continue

        # Extract or find solution
        solution = extract_or_create_solution(title, content, comments)
        if not solution or len(solution) < 80:
            continue

        # Categorize
        category = smart_categorize(title, content)

        # Create slug
        slug = slugify(title)
        if not slug or len(slug) < 5 or slug in existing:
            continue

        # Write
        cat_dir = os.path.join(SOLUTIONS_DIR, category)
        os.makedirs(cat_dir, exist_ok=True)
        filepath = os.path.join(cat_dir, f"{slug}.md")

        clean_title = re.sub(r'<[^>]+>', '', title).strip()[:100]

        md = f"""---
layout: solution
title: "{clean_title.replace('"', "'")}"
category: {category}
---

# {clean_title}

## 증상
{problem}

## 원인
{extract_cause(content)}

## 해결법
{solution}

## 참고
Moltbook 커뮤니티 토론 (submolt: {submolt}, score: {score})
"""
        with open(filepath, 'w') as f:
            f.write(md)
        written += 1
        existing.add(slug)

    print(f"Phase 3: Written {written} new solutions from category-problems.json")
    return written


def extract_real_problem(title, content):
    """Extract the concrete problem description."""
    clean = re.sub(r'<[^>]+>', '', content)

    # Look for problem sections
    for pattern in [
        r'(?:problem|issue|error|bug|symptom)[:\s]*\n([\s\S]{50,500}?)(?:\n\n\n|\n#{1,3}\s|$)',
        r'(?:What happened|What I expected|Steps to reproduce)[:\s]*\n([\s\S]{50,500}?)(?:\n\n\n|\n#{1,3}\s|$)',
    ]:
        m = re.search(pattern, clean, re.IGNORECASE)
        if m:
            return m.group(1).strip()[:400]

    # Get first meaningful paragraph
    paragraphs = [p.strip() for p in clean.split('\n\n') if len(p.strip()) > 50]
    for p in paragraphs[:3]:
        # Skip if it looks like a header or formatting
        if p.startswith('#') or p.startswith('|') or p.startswith('---'):
            continue
        return p[:400]

    return clean[:400] if len(clean) > 50 else None


def extract_cause(content):
    """Extract or infer the cause."""
    clean = re.sub(r'<[^>]+>', '', content)

    for pattern in [
        r'(?:cause|reason|why|root cause|because)[:\s]*\n?([\s\S]{30,300}?)(?:\n\n|\n#|$)',
    ]:
        m = re.search(pattern, clean, re.IGNORECASE)
        if m:
            return m.group(1).strip()[:300]

    return "아래 증상에서 추론된 원인. 상세 분석은 원본 토론 참고."


def extract_or_create_solution(title, content, comments):
    """Extract solution from content/comments, or create a specific one."""
    clean = re.sub(r'<[^>]+>', '', content)

    # Check content for embedded solutions
    for pattern in [
        r'(?:solution|fix|workaround|resolution|how to fix|here.?s how)[:\s]*\n([\s\S]{50,800}?)(?:\n\n\n|\n#{1,3}\s|$)',
        r'(?:I fixed|The fix|To fix|I solved|I resolved)[:\s]*([\s\S]{50,800}?)(?:\n\n\n|\n#{1,3}\s|$)',
        r'(?:\d\.\s+\*\*[\s\S]{20,}){2,}',  # Numbered list with bold items
    ]:
        m = re.search(pattern, clean, re.IGNORECASE)
        if m:
            fix = m.group(0) if pattern.startswith(r'(?:\d') else m.group(1)
            if len(fix.strip()) > 80:
                return fix.strip()[:800]

    # Check comments for solutions
    if not isinstance(comments, list):
        comments = []
    for comment in comments:
        comment_text = comment.get('content', '') or comment.get('body', '') or ''
        if isinstance(comment, str):
            comment_text = comment

        comment_clean = re.sub(r'<[^>]+>', '', comment_text)

        for pattern in [
            r'(?:fix|solution|try|workaround)[:\s]*([\s\S]{50,600}?)(?:\n\n|\n#|$)',
            r'(?:I found|I solved|What worked|This works)[:\s]*([\s\S]{50,600}?)(?:\n\n|\n#|$)',
        ]:
            m = re.search(pattern, comment_clean, re.IGNORECASE)
            if m:
                fix = m.group(1).strip()
                if len(fix) > 80:
                    return fix[:800]

    # Generate specific solution based on content keywords
    return generate_targeted_solution(title, content)


def generate_targeted_solution(title, content):
    """Generate a targeted (not generic) solution based on the specific problem."""
    text = f"{title} {content[:1000]}".lower()

    # Token cost specific
    if any(w in text for w in ['token cost', 'expensive', 'burning money', 'token waste', 'token limit']):
        if 'verbose' in text or 'reasoning' in text:
            return """### Verbose reasoning 토큰 낭비 해결

1. **reasoning effort 조절**: API 호출 시 `reasoning_effort: "low"` 또는 `"medium"` 설정
   ```json
   {"model": "claude-sonnet-4-6", "thinking": {"budget_tokens": 2000}}
   ```
2. **thinking budget 제한**: 최대 thinking 토큰 수를 명시적으로 제한
3. **단순 작업 분리**: 단순 작업은 thinking 비활성화 모델 사용
4. **출력 길이 제한**: `max_tokens`를 태스크에 맞게 설정"""

        if 'multi' in text and ('agent' in text or 'team' in text):
            return """### 멀티 에이전트 토큰 비용 관리

1. **통신 프로토콜 최소화**: 에이전트 간 전체 출력 대신 구조화된 JSON 요약만 전달
   ```json
   {"status": "done", "result": "bug fixed in api.py:42", "files_changed": ["api.py"]}
   ```
2. **공유 컨텍스트 풀**: 중복 없는 공유 메모리에서 읽기 (각 에이전트가 같은 파일 반복 읽기 방지)
3. **에이전트 수 최소화**: 3개 이상 에이전트는 비용 대비 효과 급감
4. **단계별 순차 실행**: 동시 실행보다 순차 파이프라인이 토큰 효율적"""

        return """### 토큰 비용 구체적 절감법

1. **프롬프트 캐싱** (Anthropic API):
   ```python
   messages = [{"role": "user", "content": [
       {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}
   ]}]
   ```
   → 캐시 히트 시 입력 토큰 비용 90% 절감

2. **모델 라우팅 자동화**:
   ```python
   def select_model(task_complexity):
       if complexity < 3: return "haiku"      # $0.25/M
       if complexity < 7: return "sonnet"     # $3/M
       return "opus"                           # $15/M
   ```

3. **컨텍스트 윈도우 감사**: `tiktoken`으로 각 요청의 토큰 수 로깅
   → 가장 비싼 요청 식별 → 최적화 우선순위"""

    # Memory / forget
    if any(w in text for w in ['forget', 'memory', 'remember', 'amnesia', 'context loss']):
        if 'continuous' in text or 'long' in text or 'days' in text:
            return """### 장기 메모리 유지 구현

1. **파일 기반 메모리**:
   ```bash
   # 세션 종료 시 자동 저장
   echo "## Session $(date +%Y%m%d)" >> ~/.agent/memory.md
   echo "- Decided: use PostgreSQL" >> ~/.agent/memory.md
   echo "- Pending: auth module review" >> ~/.agent/memory.md
   ```

2. **구조화된 상태 파일** (JSON):
   ```json
   {
     "project": "synapse-ai",
     "decisions": [{"date": "2026-03-25", "what": "REST→GraphQL", "why": "실시간 구독 필요"}],
     "current_task": "인증 모듈 구현",
     "blockers": []
   }
   ```

3. **세션 시작 시 자동 로드**: 시스템 프롬프트에 메모리 파일 자동 포함
4. **주기적 정리**: 오래된 항목 아카이브, 활성 항목만 유지"""

        return """### 에이전트 메모리 유실 방지

1. **CLAUDE.md 파일 활용**: 프로젝트 루트에 핵심 정보 영속화
   ```markdown
   # Project Context
   - DB: PostgreSQL 16, Schema in src/db/schema.sql
   - Auth: JWT + refresh tokens
   - Deploy: Docker on AWS ECS
   ```

2. **세션 요약 저장**: 각 세션 종료 시 결과를 파일로 저장
3. **명시적 handoff**: 새 세션 시작 시 이전 세션 요약 전달
4. **외부 상태**: Redis/SQLite에 에이전트 상태 저장 (세션 독립)"""

    # Hallucination
    if any(w in text for w in ['hallucin', 'making things up', 'wrong', 'incorrect', 'trust']):
        return """### 할루시네이션 감지 및 방지

1. **자동 검증 파이프라인**:
   ```python
   response = agent.generate(prompt)
   # 코드 검증
   if contains_code(response):
       result = execute_in_sandbox(response.code)
       if result.error:
           response = agent.generate(f"이 코드에 에러: {result.error}. 수정해.")
   # 사실 검증
   if contains_claims(response):
       sources = search_docs(response.claims)
       if not sources:
           response = agent.generate("출처를 찾을 수 없음. 확실한 것만 답변해.")
   ```

2. **시스템 프롬프트 설정**:
   ```
   규칙: 확실하지 않으면 "확인 필요"라고 명시.
   존재하지 않는 라이브러리/함수를 절대 만들어내지 마.
   모든 주장에 근거를 포함해.
   ```

3. **Temperature 조정**: 사실 기반 작업은 temperature=0 사용
4. **이중 확인**: 중요한 출력은 다른 모델/프롬프트로 교차 검증"""

    # Debug / troubleshoot
    if any(w in text for w in ['debug', 'troubleshoot', 'diagnos']):
        return """### 에이전트 디버깅 체계적 접근법

1. **로그 수집**: 에이전트의 모든 입출력을 파일로 기록
   ```bash
   export AGENT_LOG_LEVEL=debug
   export AGENT_LOG_FILE=~/.agent/debug.log
   ```

2. **재현 최소화**: 문제를 최소 입력으로 재현
3. **단계별 실행**: 자동 실행 대신 한 단계씩 수동 확인
4. **비교 분석**: 성공 케이스 vs 실패 케이스의 입력 차이 비교
5. **격리 테스트**: 네트워크, 파일시스템, API 각각 독립 테스트"""

    # Slow / performance
    if any(w in text for w in ['slow', 'latency', 'performance', 'speed']):
        return """### 에이전트 성능 최적화

1. **병목 측정**:
   ```python
   import time
   start = time.time()
   result = agent.step()
   print(f"Step took {time.time()-start:.2f}s")
   ```

2. **스트리밍 응답**: 전체 응답 대기 대신 스트리밍으로 즉시 출력 시작
3. **병렬 도구 호출**: 독립적 도구 호출은 `asyncio.gather()`로 동시 실행
4. **모델 다운그레이드**: 지연이 크면 더 빠른 모델 (Haiku, Flash) 사용
5. **캐싱**: 동일 입력에 대한 도구 결과를 TTL 캐싱"""

    # Config / setup
    if any(w in text for w in ['config', 'setup', 'install', 'setting']):
        return """### 설정/구성 문제 진단

1. **설정 파일 위치 확인**:
   ```bash
   # OpenClaw
   cat ~/.openclaw/config.yaml
   # Claude Code
   cat ~/.claude/settings.json
   ```

2. **환경변수 검증**:
   ```bash
   env | grep -i "ANTHROPIC\|OPENAI\|OPENCLAW"
   ```

3. **최소 설정 테스트**: 모든 커스텀 설정 제거 → 기본값으로 동작 확인 → 하나씩 추가
4. **버전 호환성**: `openclaw --version`으로 현재 버전 확인, changelog에서 breaking changes 확인
5. **로그 확인**: 시작 로그에서 `WARN`/`ERROR` 메시지 검색"""

    # Loop / stuck
    if any(w in text for w in ['loop', 'stuck', 'retry', 'repeating', 'infinite']):
        return """### 에이전트 루프/멈춤 탈출

1. **루프 감지 구현**:
   ```python
   seen_errors = []
   for attempt in range(max_attempts):
       result = agent.run()
       if result.error:
           if result.error in seen_errors:
               break  # 같은 에러 반복 → 중단
           seen_errors.append(result.error)
   ```

2. **타임아웃 설정**: 단일 작업에 절대 시간 제한
   ```python
   signal.alarm(300)  # 5분 타임아웃
   ```

3. **대안 전략 매핑**: 에러 유형별 대체 접근법 사전 정의
4. **에스컬레이션**: 3회 실패 → 사람에게 보고 + 현재 상태 덤프"""

    # Auth
    if any(w in text for w in ['auth', 'oauth', 'api key', 'permission', 'credential']):
        return """### 인증 문제 단계별 진단

1. **토큰/키 유효성**:
   ```bash
   # Anthropic
   curl -H "x-api-key: $ANTHROPIC_API_KEY" https://api.anthropic.com/v1/models
   # OpenAI
   curl -H "Authorization: Bearer $OPENAI_API_KEY" https://api.openai.com/v1/models
   ```

2. **만료 확인**: JWT 토큰은 `jwt.io`에서 exp 필드 확인
3. **스코프 확인**: OAuth 앱의 granted scopes가 필요한 권한을 포함하는지 확인
4. **환경 분리**: dev/staging/prod 환경의 키가 혼용되지 않는지 확인
5. **캐시 삭제**: `rm ~/.openclaw/credentials.json` 후 재인증"""

    # Rate limit
    if any(w in text for w in ['rate limit', '429', 'throttl', 'quota']):
        return """### Rate Limit 실전 대응

1. **Retry-After 헤더 파싱**:
   ```python
   if response.status == 429:
       wait = int(response.headers.get('Retry-After', 60))
       time.sleep(wait)
   ```

2. **지수 백오프 + 지터 구현**:
   ```python
   import random
   delay = min(2 ** attempt + random.uniform(0, 1), 120)
   ```

3. **요청 큐잉**: `asyncio.Semaphore(10)`으로 동시 요청 수 제한
4. **사용량 추적**: API 응답의 `x-ratelimit-remaining` 헤더 모니터링
5. **대체 provider**: 한 provider가 429면 다른 provider로 자동 전환"""

    return None  # No good solution possible


def smart_categorize(title, content):
    text = f"{title} {content[:500]}".lower()
    rules = {
        'token-cost': ['token cost', 'token waste', 'expensive', 'burning', 'token limit', 'billing', 'verbose reason'],
        'context-window': ['context window', 'context length', 'context overflow', 'truncat', 'conversation length'],
        'hallucination': ['hallucin', 'making things up', 'fabricat', 'wrong answer', 'incorrect', 'trust'],
        'memory': ['forget', 'amnesia', 'memory', 'lost context', 'remember', 'continuous memory'],
        'loop-stuck': ['loop', 'stuck', 'retry', 'repeating', 'infinite'],
        'auth': ['oauth', 'api key', 'credential', 'permission', 'auth', '401', '403'],
        'rate-limit': ['rate limit', '429', 'throttl', 'quota'],
        'config': ['config', 'setup', 'install', 'setting', 'yaml', 'env'],
        'performance': ['slow', 'latency', 'performance', 'timeout', 'speed'],
        'tool-failure': ['tool fail', 'mcp', 'plugin', 'function call'],
        'docker': ['docker', 'container', 'sandbox'],
        'telegram': ['telegram', 'tg bot'],
        'concurrency': ['race condition', 'deadlock', 'concurrent', 'async'],
        'prompt-engineering': ['prompt injection', 'jailbreak', 'prompt'],
        'openclaw': ['openclaw', 'clawhub', 'gateway', 'skill'],
    }
    scores = {}
    for cat, kws in rules.items():
        scores[cat] = sum(len(kw) for kw in kws if kw in text)
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


def main():
    before = sum(1 for r, d, f in os.walk(SOLUTIONS_DIR)
                 for fn in f if fn.endswith('.md') and fn not in ('README.md', 'TEMPLATE.md'))
    print(f"Before: {before} solutions")

    # Phase 1: Delete moltbook
    phase1_delete_moltbook()

    # Phase 2: Delete generic
    phase2_delete_generic()

    mid = sum(1 for r, d, f in os.walk(SOLUTIONS_DIR)
              for fn in f if fn.endswith('.md') and fn not in ('README.md', 'TEMPLATE.md'))
    print(f"After cleanup: {mid} solutions")

    # Phase 3: Add from category-problems
    phase3_process_category_problems()

    # Final count
    after = sum(1 for r, d, f in os.walk(SOLUTIONS_DIR)
                for fn in f if fn.endswith('.md') and fn not in ('README.md', 'TEMPLATE.md'))

    print(f"\n=== Final ===")
    print(f"Before: {before}")
    print(f"After cleanup: {mid}")
    print(f"After new additions: {after}")
    print(f"Removed: {before - mid}")
    print(f"Added: {after - mid}")

    print(f"\nCategory distribution:")
    for cat_dir in sorted(os.listdir(SOLUTIONS_DIR)):
        cat_path = os.path.join(SOLUTIONS_DIR, cat_dir)
        if os.path.isdir(cat_path):
            count = len([f for f in os.listdir(cat_path) if f.endswith('.md')])
            if count > 0:
                print(f"  {cat_dir}: {count}")


if __name__ == "__main__":
    main()
