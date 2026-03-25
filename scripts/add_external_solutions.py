#!/usr/bin/env python3
"""Generate high-quality solutions from web research and GitHub Issues."""

import os
import re
import json
import subprocess

SOLUTIONS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "_solutions")

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

def write_solution(category, slug, title, symptom, cause, fix, source, tokens_waste="5,000~15,000", tokens_fix="500"):
    cat_dir = os.path.join(SOLUTIONS_DIR, category)
    os.makedirs(cat_dir, exist_ok=True)
    filepath = os.path.join(cat_dir, f"{slug}.md")
    if os.path.exists(filepath):
        return False
    clean_title = title.replace('"', "'")
    content = f"""---
layout: solution
title: "{clean_title}"
category: {category}
source: {source}
---

# {title}

## 증상
{symptom}

## 원인
{cause}

## 해결법
{fix}

## 예상 토큰 절약
이 에러로 삽질 시: 약 {tokens_waste} 토큰 소비
이 해결법 참조 시: 약 {tokens_fix} 토큰

## 출처
{source}
"""
    with open(filepath, 'w') as f:
        f.write(content)
    return True


def add_web_research_solutions(existing):
    """Solutions from web articles about AI agent problems."""
    written = 0
    solutions = [
        # From Stevens article - AI Agent Economics
        {
            'category': 'token-cost',
            'slug': 'quadratic-token-growth-multi-turn-agents',
            'title': 'Quadratic token cost growth in multi-turn agent loops',
            'symptom': 'Agent costs escalate rapidly during multi-turn conversations. A single task that should cost $0.50 ends up costing $5-8. Token usage grows quadratically because each turn resends the entire conversation history.',
            'cause': 'LLMs charge for every input token in every turn. In multi-turn agent loops, the full conversation history is sent with each new turn, causing costs to grow quadratically rather than linearly. An unconstrained agent can cost $5-8 per software engineering task.',
            'fix': """### Quadratic cost growth 해결법

1. **프롬프트 캐싱 활용**
   - 동일한 시스템 프롬프트/지시문은 캐싱하면 입력 비용 ~90% 절감
   - 대부분의 주요 API 제공자가 prompt caching 지원
   ```
   # Anthropic API example
   cache_control: {"type": "ephemeral"}
   ```

2. **동적 턴 제한 설정**
   - 성공 확률 기반으로 턴 수 제한 → 비용 24% 절감 + 성능 유지
   - 3회 실패 시 다른 접근법으로 전환

3. **컨텍스트 윈도우 관리**
   - 이전 턴의 전체 출력 대신 요약만 유지
   - 도구 호출 결과는 핵심 정보만 보존

4. **라우팅 패턴**
   - 작업 복잡도에 따라 모델 선택 자동화
   - 단순 작업: Haiku ($0.25/M), 복잡한 작업: Opus ($15/M)""",
            'source': 'Stevens Institute - Hidden Economics of AI Agents',
            'tokens_waste': '50,000~200,000',
            'tokens_fix': '5,000',
        },
        {
            'category': 'loop-stuck',
            'slug': 'agent-stuck-repeating-failed-action',
            'title': 'Agent stuck repeating the same failed action (looping)',
            'symptom': 'Agent attempts the same failing approach repeatedly without trying alternatives. Gets stuck in a retry loop, burning tokens on identical failed attempts. No exit condition triggers.',
            'cause': 'Lack of dynamic exit criteria or state tracking. The agent has no mechanism to detect that it has already tried an approach and failed, so it keeps attempting the same action.',
            'fix': """### 에이전트 루프 탈출법

1. **실패 히스토리 추적**
   ```python
   failed_approaches = []
   for attempt in range(max_attempts):
       approach = select_approach(exclude=failed_approaches)
       result = try_approach(approach)
       if result.failed:
           failed_approaches.append(approach)
       else:
           break
   ```

2. **동적 턴 제한**
   - 성공 확률 기반 동적 제한: 성공률 < 20%이면 중단
   - 연구 결과: 이 방식으로 비용 24% 절감 + 성능 유지

3. **에러 패턴 감지**
   - 같은 에러 메시지 3회 연속 → 자동 전환
   - 에러 유형별 대체 전략 매핑

4. **에스컬레이션**
   - 자동 해결 실패 시 사람에게 즉시 보고
   - "모르면 OK 처리하지 않는다" 원칙""",
            'source': 'Stevens Institute - Hidden Economics of AI Agents',
            'tokens_waste': '20,000~50,000',
            'tokens_fix': '2,000',
        },
        {
            'category': 'hallucination',
            'slug': 'agent-invents-false-facts-code-libraries',
            'title': 'Agent invents non-existent code libraries or false facts',
            'symptom': 'Agent confidently references libraries, functions, or APIs that do not exist. Generates code using fabricated package names. Provides false information about configurations or settings.',
            'cause': 'LLM probabilistic nature generates plausible-sounding but incorrect information. Without grounding in verified data, the model fills gaps with statistically likely but fictional content.',
            'fix': """### 할루시네이션 방지 실전 가이드

1. **검증 루프 구현**
   ```
   생성 → 검증 → 수정 → 재검증
   ```
   - 코드: 생성 후 반드시 실행/컴파일 확인
   - 패키지: `pip show` / `npm ls`로 실존 확인
   - API: 공식 문서와 대조

2. **"모르면 모른다" 시스템 프롬프트**
   ```
   확실하지 않은 정보는 절대 추측하지 마.
   모르면 "확인 필요"라고 명시하고 검색해.
   ```

3. **RAG (Retrieval-Augmented Generation)**
   - 공식 문서를 벡터 DB에 인덱싱
   - 답변 전 관련 문서 검색 → 검색 결과 기반 답변

4. **출력 검증 자동화**
   - import 구문의 패키지명 → 실존 확인
   - URL → HTTP HEAD 요청으로 존재 확인
   - 설정값 → 공식 스키마와 대조""",
            'source': 'Stevens Institute + PR-Peri Hallucination Research',
            'tokens_waste': '10,000~30,000',
            'tokens_fix': '2,000',
        },
        {
            'category': 'tool-failure',
            'slug': 'agent-calls-api-with-wrong-schema',
            'title': 'Agent calls APIs with incorrect schemas or parameters',
            'symptom': 'Agent invokes tools/APIs with wrong parameter types, missing required fields, or incorrect schemas. API calls return validation errors (400, 422) repeatedly.',
            'cause': 'Insufficient grounding in tool specifications. Agent relies on training data rather than actual API documentation, leading to outdated or incorrect parameter usage.',
            'fix': """### API/도구 호출 실패 해결

1. **도구 스펙 명시적 주입**
   - 시스템 프롬프트에 정확한 API 스키마 포함
   - JSON Schema 형태로 파라미터 타입/필수값 명시

2. **요청 전 검증**
   ```python
   # 호출 전 스키마 검증
   from jsonschema import validate
   validate(instance=params, schema=tool_schema)
   ```

3. **에러 피드백 루프**
   - 422 에러 응답의 validation 메시지를 에이전트에게 피드백
   - "이 필드가 필수입니다" → 재시도 시 포함

4. **멀티 스텝 계획**
   - 도구 호출 전 계획 단계 추가
   - 계획 → 파라미터 확인 → 실행 → 검증""",
            'source': 'Stevens Institute - Hidden Economics of AI Agents',
        },
        # From SparkCo - Context Windows 2026
        {
            'category': 'context-window',
            'slug': 'lost-customer-context-across-sessions',
            'title': 'Agent loses all context when starting a new session',
            'symptom': 'Agent cannot recall information from previous interactions. User must repeat explanations and context every new session. Multi-step workflows break across session boundaries.',
            'cause': 'Stateless pipelines drop multi-step task context. Ephemeral memory is capped at 128k tokens and provides no persistence across sessions.',
            'fix': """### 세션 간 컨텍스트 유지

1. **영속적 메모리 파일**
   - CLAUDE.md / AGENTS.md에 핵심 정보 기록
   - 프로젝트별 `.memory/` 디렉토리에 상태 저장

2. **세션 요약 자동 저장**
   ```
   세션 종료 시:
   1. 주요 결정사항 요약
   2. 현재 진행상황 기록
   3. 다음 세션에서 할 일 목록
   → session-summary.md에 저장
   ```

3. **메모리 스티칭**
   - 지식 그래프로 세션 간 엔티티 연결
   - 이전 세션 요약을 새 세션 컨텍스트에 자동 주입

4. **외부 상태 관리**
   - JSON/SQLite에 에이전트 상태 저장
   - 세션 시작 시 마지막 상태 로드""",
            'source': 'SparkCo - Agent Context Windows in 2026',
            'tokens_waste': '15,000~30,000',
            'tokens_fix': '2,000',
        },
        {
            'category': 'context-window',
            'slug': 'memory-decay-attention-dilution-long-context',
            'title': '40% reasoning accuracy drop beyond 50k tokens (attention dilution)',
            'symptom': 'Agent reasoning quality degrades significantly in long conversations. After 50k+ tokens, the agent starts making mistakes, forgetting instructions, and giving inconsistent answers. 40% accuracy degradation measured.',
            'cause': 'Attention dilution in long contexts. Transformer models dilute their focus across expanded sequences. Model pruning and cost-driven windowing exacerbate the problem. Bigger context windows actually make hallucination rates WORSE.',
            'fix': """### 긴 컨텍스트 품질 유지

1. **선택적 컨텍스트 주입**
   - 전체 히스토리의 5-10%만 선택적으로 포함 → 토큰 40-70% 절감
   - 최근 5-10개 메시지만 전체 유지, 나머지는 요약

2. **리랭킹으로 관련성 우선**
   - Cross-encoder 리랭킹으로 가장 관련 있는 컨텍스트 우선 배치
   - 최신성 + 관련성 가중치 적용

3. **티어드 스토리지**
   - Hot (메모리): 현재 대화 (최근 5턴)
   - Warm (인덱스): 이번 세션 요약
   - Cold (파일): 과거 세션 전체

4. **주기적 컨텍스트 리프레시**
   - 20턴마다 컨텍스트 요약 + 재시작
   - 핵심 지시사항은 항상 프롬프트 시작에 배치""",
            'source': 'SparkCo - Agent Context Windows in 2026',
            'tokens_waste': '30,000~100,000',
            'tokens_fix': '3,000',
        },
        {
            'category': 'hallucination',
            'slug': 'hallucinated-state-transitions-false-memory',
            'title': 'Agent fabricates memories of previous interactions that never happened',
            'symptom': 'Agent claims to remember previous conversations or states that never occurred. Makes decisions based on false memories. "Last time we discussed X" when X never happened.',
            'cause': 'Stateless designs lead to hallucinated state transitions. Without persistent records to ground reasoning, the model generates plausible-sounding but fictional memories of prior interactions.',
            'fix': """### 가짜 기억 방지

1. **불변 메모리 로그**
   - 모든 상호작용을 변경 불가능한 로그에 기록
   - "기억"은 반드시 로그에서 검색 → 없으면 "기록 없음" 반환

2. **명시적 상태 확인**
   ```
   시스템 프롬프트:
   "이전 대화를 추측하지 마. 기록된 것만 참조해.
   기록이 없으면 '이전 기록이 없습니다'라고 답해."
   ```

3. **엔티티 해상도 그래프**
   - 사용자/프로젝트/결정 등을 그래프로 관리
   - 각 노드에 타임스탬프 + 출처 기록

4. **감사 추적**
   - 모든 에이전트 결정에 근거 명시 요구
   - "이 결정의 근거: [세션 #12, 2026-03-20]" 형태""",
            'source': 'SparkCo - Agent Context Windows in 2026',
        },
        {
            'category': 'token-cost',
            'slug': 'cost-induced-context-pruning-hallucination',
            'title': 'Aggressive context pruning to save costs causes 50% more hallucinations',
            'symptom': 'To reduce token costs, system aggressively removes older context. But this causes agent to hallucinate more frequently — 50% increase in hallucination rate. Key business logic and constraints get lost.',
            'cause': 'Evicting tokens to cut expenses omits key details needed for accurate reasoning. The cost-saving measure backfires by creating more errors that require more tokens to fix.',
            'fix': """### 비용 절감 vs 품질 균형

1. **정책 기반 보존**
   - 모든 컨텍스트를 동등하게 취급하지 않기
   - 우선순위: 시스템 지시 > 사용자 제약조건 > 최근 대화 > 과거 대화

2. **티어드 스토리지**
   - Hot (in-memory): 활성 컨텍스트 → 빠른 접근
   - Cold (archival): 전체 히스토리 → 필요시 검색
   - 비용 효율적으로 전체 히스토리 유지

3. **스마트 요약**
   - 삭제 대신 요약으로 대체
   - 핵심 결정/제약조건은 요약에 반드시 포함

4. **비용 모니터링 + 알림**
   - 토큰 사용량 실시간 추적
   - 임계값 초과 시 사람에게 알림 (자동 삭제 X)""",
            'source': 'SparkCo - Agent Context Windows in 2026',
            'tokens_waste': '20,000~50,000',
            'tokens_fix': '3,000',
        },
        # From Hallucination research
        {
            'category': 'hallucination',
            'slug': 'retrieval-failure-vector-search-miss',
            'title': 'RAG vector search fails to find correct document, agent guesses instead',
            'symptom': 'Agent has access to a knowledge base but gives wrong answers because the vector search retrieves irrelevant documents. The correct answer exists in the database but is not found.',
            'cause': 'Vector search fails to locate the correct information. Poor chunking strategy, missing metadata, duplicate chunks, or suboptimal embedding model.',
            'fix': """### RAG 검색 실패 해결

1. **시맨틱 청킹**
   - 고정 크기 대신 의미 단위로 분할
   - 청크 오버랩 추가 (경계에서 정보 손실 방지)
   - 문서 제목/헤딩을 청크 텍스트에 포함

2. **중복 제거**
   - 중복 청크 제거 → 검색 정확도 향상

3. **메타데이터 필터링**
   - 버전, 제품, 언어, 날짜 등 메타데이터 활용
   - 검색 시 메타데이터 필터로 범위 좁히기

4. **리랭킹**
   - Bi-encoder로 후보 검색 → Cross-encoder로 상위 5-8개 선별
   - 양보다 질: 50개 청크보다 5개 정확한 청크""",
            'source': 'Perivitta Rajendran - Why Hallucination Happens',
        },
        {
            'category': 'hallucination',
            'slug': 'noisy-context-overload-too-many-chunks',
            'title': 'Too many RAG chunks cause conflicting information and wrong answers',
            'symptom': 'Inserting 20-50+ document chunks into prompt causes model to give wrong answers. Model blends conflicting sources or prioritizes wrong sections.',
            'cause': 'Context overload with conflicting and repeated information. Model cannot distinguish between relevant and irrelevant chunks when too many are included.',
            'fix': """### 컨텍스트 과부하 해결

1. **청크 수 제한**: 후보 많이 검색하되 최종 5-8개만 프롬프트에 포함
2. **리랭킹 필수**: 검색 후 cross-encoder로 관련성 재평가
3. **충돌 감지**: 같은 주제의 상충 정보 자동 감지 → 최신/권위 있는 것 우선
4. **포맷팅**: 핵심 구절을 하이라이트해서 모델이 집중하도록 유도""",
            'source': 'Perivitta Rajendran - Why Hallucination Happens',
        },
        {
            'category': 'hallucination',
            'slug': 'lost-in-the-middle-effect',
            'title': 'LLM ignores information placed in the middle of long prompts',
            'symptom': 'Agent misses critical information that is placed in the middle of the prompt. Answers based only on beginning/end of context, ignoring middle sections.',
            'cause': 'Lost-in-the-middle effect: LLMs perform best when relevant information appears near the beginning or end of the prompt. Information placed in the middle is often overlooked.',
            'fix': """### "Lost in the Middle" 효과 해결

1. **핵심 정보 위치 전략**
   - 가장 중요한 정보 → 프롬프트 시작 부분
   - 두 번째로 중요한 → 프롬프트 끝 부분
   - 보조 정보만 중간에 배치

2. **청크 수 최소화**: 전송 청크 수를 줄여 중간 영역 자체를 축소

3. **명시적 포맷팅**
   ```
   === CRITICAL INFORMATION ===
   [핵심 정보]
   === END CRITICAL ===
   ```

4. **분할 질의**: 긴 프롬프트 대신 여러 짧은 질의로 분할""",
            'source': 'Perivitta Rajendran - Why Hallucination Happens',
        },
        {
            'category': 'hallucination',
            'slug': 'version-source-mixing-hybrid-answers',
            'title': 'RAG mixes different document versions creating incorrect hybrid answers',
            'symptom': 'Agent combines information from v1.0 and v2.0 documentation, creating answers that match neither version. Configuration syntax from old version mixed with new version features.',
            'cause': 'Multiple versions of same documentation exist in vector database. Model combines chunks from different versions into factually incorrect hybrids.',
            'fix': """### 버전 혼합 방지

1. **메타데이터 버전 태깅**: 모든 청크에 버전 번호 메타데이터 부여
2. **검색 시 버전 필터**: 특정 버전만 검색하도록 메타데이터 필터 적용
3. **벡터 DB 분리**: 다른 버전은 다른 컬렉션/인덱스에 저장
4. **청크 내 버전 표기**: 청크 텍스트 자체에 "v2.0:" 등 버전 식별자 포함""",
            'source': 'Perivitta Rajendran - Why Hallucination Happens',
        },
        {
            'category': 'memory',
            'slug': 'conversation-history-overflow-truncation',
            'title': 'Chat history grows until truncated, losing critical early context',
            'symptom': 'Long conversations hit context window limit. System auto-truncates older messages. Agent loses crucial earlier instructions, constraints, or decisions.',
            'cause': 'Chat history grows indefinitely until it exceeds context limits. Automatic truncation removes oldest messages regardless of importance.',
            'fix': """### 대화 히스토리 오버플로우 해결

1. **슬라이딩 윈도우 + 요약**
   - 최근 5-10개 메시지: 전체 보존
   - 이전 메시지: 자동 요약으로 압축
   - 핵심 제약조건/결정: 별도 "pinned context"로 보존

2. **구조화된 메모리**
   ```json
   {
     "constraints": ["반드시 Python 3.12 사용", "MIT 라이선스"],
     "decisions": ["REST API 대신 GraphQL 선택"],
     "current_task": "사용자 인증 구현 중",
     "recent_messages": [...]
   }
   ```

3. **중요도 기반 보존**
   - 시스템 지시: 절대 삭제 안 함
   - 사용자 제약: 절대 삭제 안 함
   - 도구 출력: 요약 후 원본 삭제
   - 일반 대화: 오래되면 요약 후 삭제""",
            'source': 'Perivitta Rajendran - Why Hallucination Happens',
            'tokens_waste': '15,000~40,000',
            'tokens_fix': '2,000',
        },
        {
            'category': 'hallucination',
            'slug': 'model-answers-despite-weak-evidence',
            'title': 'Agent confidently answers when it should say "I don\'t know"',
            'symptom': 'Agent provides answers even when retrieval signals are weak or no relevant documents are found. Generates plausible-sounding but incorrect responses instead of admitting uncertainty.',
            'cause': 'Model trained to be helpful defaults to providing responses even when evidence is marginal. No confidence threshold is set for retrieval quality.',
            'fix': """### 불확실할 때 답변 거부하도록 설정

1. **유사도 임계값 설정**
   ```python
   if max_similarity_score < 0.7:
       return "관련 정보를 찾지 못했습니다. 다시 검색하거나 질문을 구체화해 주세요."
   ```

2. **시스템 프롬프트 명시**
   ```
   확실하지 않으면 절대 추측하지 마.
   검색 결과가 없으면 "해당 정보를 찾을 수 없습니다"라고 답해.
   추측으로 답변하는 것보다 모른다고 하는 것이 낫다.
   ```

3. **다단계 확인**
   - 1차: 검색 → 결과 없으면 "정보 없음"
   - 2차: 결과 있어도 유사도 낮으면 "확실하지 않음" 표시
   - 3차: 높은 유사도일 때만 확정 답변""",
            'source': 'Perivitta Rajendran - Why Hallucination Happens',
        },
    ]

    for sol in solutions:
        if sol['slug'] in existing:
            continue
        ok = write_solution(
            sol['category'], sol['slug'], sol['title'],
            sol['symptom'], sol['cause'], sol['fix'],
            sol['source'],
            sol.get('tokens_waste', '5,000~15,000'),
            sol.get('tokens_fix', '500'),
        )
        if ok:
            written += 1
            existing.add(sol['slug'])

    return written


def add_github_issues_solutions(existing):
    """Add solutions from specific GitHub Issues with detailed content."""
    written = 0

    # Curated high-value issues with specific solutions
    issues = [
        {
            'category': 'openclaw',
            'slug': 'llm-request-failed-risc-v64',
            'title': 'OpenClaw returns "run Error: LLM Request Failed" on RISC-V64',
            'symptom': 'OpenClaw fails to make LLM requests on RISC-V64 architecture. Error message: "run Error: LLM Request Failed". Agent cannot function at all on this platform.',
            'cause': 'OpenClaw dependencies may not have RISC-V64 native builds. TLS/crypto libraries or Node.js native addons may be incompatible with the architecture.',
            'fix': """### RISC-V64 LLM 요청 실패 해결
1. Node.js가 RISC-V64에서 공식 지원되는 버전인지 확인 (v20+ 권장)
2. `--openssl-no-asm` 플래그로 Node.js 빌드
3. native 모듈 재컴파일: `npm rebuild`
4. 대안: x86_64 Docker 컨테이너에서 QEMU로 실행""",
            'source': 'https://github.com/openclaw/openclaw/issues/54253',
        },
        {
            'category': 'token-cost',
            'slug': 'token-usage-data-drops-between-stream-persist',
            'title': 'Token usage data drops between streaming and session persistence',
            'symptom': 'Token usage metrics are lost between the streaming response and session storage. Dashboard shows 0 tokens for sessions that actually consumed tokens. Cannot track real costs.',
            'cause': 'pi-ai stream response does not correctly pass usage metadata to the session persistence layer. Field name mismatch between provider response and storage schema.',
            'fix': """### 토큰 사용량 추적 누락 해결
1. Provider 응답의 usage 필드 매핑 확인
2. `usageMetadata` vs `usage` 필드명 불일치 패치
3. 세션 저장 시 usage 데이터가 null이면 로그 경고
4. 수동 추적: API 응답 헤더의 usage 정보 직접 파싱""",
            'source': 'https://github.com/openclaw/openclaw/issues/54218',
        },
        {
            'category': 'auth',
            'slug': 'google-provider-authheader-not-working',
            'title': 'Google provider authHeader:true returns 400 Missing Authentication',
            'symptom': 'Setting `authHeader: true` in Google provider config causes all API calls to return 400 "Missing or invalid Authentication". Standard API key authentication fails.',
            'cause': 'The authHeader flag is not correctly injecting the Authorization header for Google API requests. The provider implementation may expect a different header format.',
            'fix': """### Google Provider 인증 실패 해결
1. `authHeader: true` 대신 API 키를 query parameter로 전달
2. 설정에서 `authHeader` 제거하고 `apiKey` 직접 지정
3. Google API는 `Authorization: Bearer` 대신 `x-goog-api-key` 헤더 사용
4. 환경변수 확인: `GOOGLE_API_KEY` 또는 `GEMINI_API_KEY` 설정""",
            'source': 'https://github.com/openclaw/openclaw/issues/54175',
        },
        {
            'category': 'auth',
            'slug': 'clawhub-login-loop-after-account-deletion',
            'title': 'ClawHub login loop after account deletion — OAuth completes but no redirect',
            'symptom': 'After deleting and recreating account, ClawHub login enters infinite loop. OAuth flow completes successfully but redirect back to ClawHub never happens. Stuck on loading screen.',
            'cause': 'Stale OAuth tokens/sessions cached from the deleted account conflict with new account. Browser cookies and local storage retain old session data.',
            'fix': """### ClawHub 로그인 루프 해결
1. 브라우저 쿠키 + 로컬 스토리지 전체 삭제 (ClawHub 도메인)
2. `~/.openclaw/credentials.json` 삭제
3. GitHub OAuth 앱에서 이전 권한 취소 (Settings → Applications)
4. 시크릿/프라이빗 브라우저 창에서 재로그인 시도""",
            'source': 'https://github.com/openclaw/openclaw/issues/54076',
        },
        {
            'category': 'rate-limit',
            'slug': 'tui-surfaces-rate-limit-despite-fallback',
            'title': 'TUI shows rate limit error even though fallback model succeeds',
            'symptom': 'After primary model (openai-codex) hits rate limit, fallback model processes request successfully. But TUI still displays rate limit error to user, causing confusion.',
            'cause': 'Error from primary model is surfaced to UI before fallback completes. Error handling does not suppress primary error when fallback succeeds.',
            'fix': """### Rate limit 폴백 에러 표시 해결
1. 설정에서 `suppressFallbackErrors: true` 추가 (지원되는 경우)
2. 대안: 기본 모델을 rate limit이 높은 모델로 변경
3. Rate limit 예방: 요청 간 최소 간격 설정
4. 에러가 보이더라도 실제 응답은 정상 — UI 버그로 무시 가능""",
            'source': 'https://github.com/openclaw/openclaw/issues/54060',
        },
        {
            'category': 'auth',
            'slug': 'stale-oauth-credentials-telegram-refresh-fail',
            'title': 'OAuth sync imports stale credentials, Telegram fails with refresh_token error',
            'symptom': 'OpenAI Codex OAuth sync appears to import stale credentials. Telegram channel then fails with refresh_token_not_found error. Multiple channels break simultaneously.',
            'cause': 'OAuth credential sync pulls expired tokens from cache. Gateway uses these stale tokens and fails to refresh, cascading failures to dependent channels.',
            'fix': """### OAuth 인증 정보 동기화 실패 해결
1. 캐시된 토큰 삭제: `~/.openclaw/oauth-tokens/` 디렉토리 비우기
2. Gateway 재시작 전 `OPENCLAW_OAUTH_CACHE=false` 환경변수 설정
3. Telegram 토큰 재발급: BotFather에서 새 토큰 생성
4. 수동 재인증: `openclaw auth refresh --force`""",
            'source': 'https://github.com/openclaw/openclaw/issues/54050',
        },
        {
            'category': 'memory',
            'slug': 'memory-index-0-chunks-after-update',
            'title': 'Memory index shows 0 chunks after update, memory_search returns empty',
            'symptom': 'After updating to 2026.3.23-2, memory index shows 0/10 files indexed. memory_search always returns empty results. Memory features completely broken.',
            'cause': 'Update changed memory indexing format or path. Existing index incompatible with new version. Migration script not automatically run.',
            'fix': """### 메모리 인덱스 0건 해결
1. 메모리 인덱스 재빌드: `openclaw memory reindex`
2. 인덱스 파일 삭제 후 재시작: `rm -rf ~/.openclaw/memory-index/`
3. 메모리 디렉토리 권한 확인: `ls -la ~/.openclaw/memory/`
4. 로그 확인: `openclaw logs --filter memory` 로 구체적 에러 확인""",
            'source': 'https://github.com/openclaw/openclaw/issues/53955',
        },
        {
            'category': 'openclaw',
            'slug': 'mcp-server-unavailable-forever-after-transient',
            'title': 'MCP server stays unavailable forever after transient startup failure',
            'symptom': 'MCP server encounters a brief startup failure (network timeout, etc). After the transient error resolves, the MCP server remains marked as "unavailable" indefinitely. Requires full gateway restart.',
            'cause': 'No retry/recovery mechanism for MCP server initialization. Once marked unavailable, status is never rechecked. Existing sessions cannot reconnect.',
            'fix': """### MCP 서버 영구 불가용 해결
1. Gateway 재시작: `openclaw gateway restart`
2. MCP 서버 상태 수동 리셋: MCP 설정에서 서버 제거 후 재추가
3. 자동 복구 설정 (지원 시): healthcheck 간격을 설정
4. 임시 해결: 새 세션을 시작하면 MCP 서버 재연결 시도""",
            'source': 'https://github.com/openclaw/openclaw/issues/53864',
        },
        {
            'category': 'loop-stuck',
            'slug': 'nostr-channel-restart-loop-no-error',
            'title': 'Nostr channel restart loop — starts and immediately stops without error',
            'symptom': 'Nostr channel provider enters infinite restart cycle. Starts up, immediately stops, starts again. No error message logged. Channel never becomes functional.',
            'cause': 'Silent failure in Nostr connection handshake. Provider starts, fails to connect, exits cleanly (no error), gateway auto-restarts it, creating infinite loop.',
            'fix': """### 채널 무한 재시작 루프 해결
1. 디버그 로깅 활성화: `OPENCLAW_LOG_LEVEL=debug`
2. Nostr relay URL이 접근 가능한지 확인: `curl -I wss://relay-url`
3. 재시작 제한 설정: `maxRestarts: 3` (지원 시)
4. 수동으로 채널 비활성화 후 원인 해결 뒤 재활성화""",
            'source': 'https://github.com/openclaw/openclaw/issues/53858',
        },
        {
            'category': 'token-cost',
            'slug': 'tool-results-duplicated-3x-token-overhead',
            'title': 'Tool results duplicated in session context — 3x token overhead',
            'symptom': 'Tool call results are stored 3 times in session context when using OmniRoute gateway. Token usage is 3x expected. Costs triple for every tool-using conversation.',
            'cause': 'OmniRoute gateway duplicates tool results in the message history. Each tool response appears in: original response, session history, and gateway cache.',
            'fix': """### 도구 결과 중복 저장 해결
1. OmniRoute 설정에서 `deduplicateToolResults: true` 활성화
2. 세션 히스토리 정리: 중복 tool_result 메시지 제거 스크립트 실행
3. 대안: OmniRoute 대신 직접 provider 연결
4. 임시 해결: 세션 길이를 짧게 유지하여 중복 누적 방지""",
            'source': 'https://github.com/openclaw/openclaw/issues/53803',
            'tokens_waste': '30,000~90,000',
            'tokens_fix': '1,000',
        },
        {
            'category': 'loop-stuck',
            'slug': 'whatsapp-infinite-loop-echo-not-filtered',
            'title': 'WhatsApp group echo creates infinite loop with bot mention',
            'symptom': 'In WhatsApp group, bot sends reply → group echoes it back with fromMe flag → bot treats it as new mention → replies again → infinite loop.',
            'cause': 'fromMe echo in WhatsApp groups is not filtered. When bot replies in a group, the message echoes back and implicit reply-to-bot mention triggers another response.',
            'fix': """### WhatsApp 무한 루프 해결
1. `filterFromMe: true` 설정 추가 (WhatsApp 플러그인 설정)
2. 메시지 ID 기반 중복 감지: 최근 응답한 메시지 ID 캐싱
3. 최소 응답 간격 설정: 같은 그룹에 5초 이내 재응답 차단
4. 그룹 설정에서 멘션만 응답하도록 변경 (에코 무시)""",
            'source': 'https://github.com/openclaw/openclaw/issues/53386',
        },
        {
            'category': 'loop-stuck',
            'slug': 'gemini-thinking-content-loops-on-model-switch',
            'title': 'Gemini 2.5 Pro thinking content loops infinitely on model switch',
            'symptom': 'Switching to Gemini 2.5 Pro causes agent to enter infinite thinking loop. "Thinking..." indicator never resolves. Agent produces no output.',
            'cause': 'Gemini 2.5 Pro thinking/reasoning content format incompatible with existing message handling. Model switch does not reset thinking state.',
            'fix': """### Gemini 모델 전환 루프 해결
1. 모델 전환 전 현재 세션 종료: `/new` 명령으로 새 세션 시작
2. thinking 모드 비활성화: `thinkingMode: false` 설정
3. Gemini 2.5 Pro 대신 Gemini 2.5 Flash 사용 (thinking 문제 없음)
4. 세션 파일 삭제: `~/.openclaw/sessions/` 해당 세션 제거 후 재시작""",
            'source': 'https://github.com/openclaw/openclaw/issues/53537',
        },
        {
            'category': 'rate-limit',
            'slug': 'anthropic-rate-limit-propagates-to-google-fallback',
            'title': 'Anthropic rate limit cooldown blocks independent Google fallback',
            'symptom': 'When Anthropic provider hits rate limit, the cooldown period incorrectly propagates to the independent Google Vertex fallback provider. Both providers become unavailable.',
            'cause': 'Rate limit cooldown is applied globally instead of per-provider. All providers share the same cooldown state.',
            'fix': """### Provider 간 Rate limit 전파 해결
1. Provider별 독립적 rate limit 설정 확인
2. `perProviderCooldown: true` 설정 (지원 시)
3. 임시 해결: Anthropic과 Google을 별도 인스턴스로 분리
4. Gateway 설정에서 fallback chain 대신 독립적 provider 라우팅 사용""",
            'source': 'https://github.com/openclaw/openclaw/issues/53233',
        },
        {
            'category': 'config',
            'slug': 'gateway-silent-fail-legacy-env-variables',
            'title': 'Gateway silently fails when using legacy CLAWDBOT_* env variables',
            'symptom': 'Gateway starts but does not function correctly. No error messages. All features appear broken. Environment variables set but not being used.',
            'cause': 'Legacy CLAWDBOT_* environment variable names are no longer recognized. New versions use OPENCLAW_* prefix. No deprecation warning is shown.',
            'fix': """### 레거시 환경변수 전환
1. 모든 `CLAWDBOT_*` 환경변수를 `OPENCLAW_*`로 변경:
   ```
   CLAWDBOT_TOKEN → OPENCLAW_TOKEN
   CLAWDBOT_CONFIG_DIR → OPENCLAW_CONFIG_DIR
   ```
2. `.env` 파일 전체 검색/치환
3. systemd service 파일도 업데이트
4. Docker compose의 environment 섹션도 확인""",
            'source': 'https://github.com/openclaw/openclaw/issues/53482',
        },
        {
            'category': 'auth',
            'slug': 'setup-token-silently-truncated-paste',
            'title': 'Setup token silently truncated when terminal line-wraps during paste',
            'symptom': 'Pasting Anthropic setup token in terminal appears to work, but authentication fails. Token is silently truncated because terminal wraps long lines and clipboard paste gets cut off.',
            'cause': 'Terminal line-wrap during paste operation truncates the token string. No validation warns about incorrect token length.',
            'fix': """### 설정 토큰 붙여넣기 잘림 해결
1. 환경변수로 설정 (붙여넣기 불필요):
   ```bash
   export ANTHROPIC_API_KEY="sk-..."
   ```
2. 파일로 전달: 토큰을 파일에 저장 후 `cat file | openclaw setup`
3. 터미널 설정: 줄바꿈 없이 긴 줄 표시하도록 설정
4. 토큰 길이 수동 확인: `echo -n "$TOKEN" | wc -c` (예상 길이와 비교)""",
            'source': 'https://github.com/openclaw/openclaw/issues/53464',
        },
        {
            'category': 'openclaw',
            'slug': 'context-caching-almost-always-doesnt-work',
            'title': 'Context caching almost always does not work',
            'symptom': 'Context caching feature is enabled but cache hit rate is near 0%. Every request is processed from scratch. No cost savings from caching.',
            'cause': 'Cache key generation is too strict. Minor variations in context (timestamps, dynamic content) cause cache misses. Cache TTL may be too short.',
            'fix': """### 컨텍스트 캐싱 미작동 해결
1. 캐시 키에서 동적 요소 제외: 타임스탬프, 랜덤값 등 제거
2. 시스템 프롬프트를 고정 문자열로 유지 (변수 치환 최소화)
3. cache TTL 증가: 기본값에서 최소 1시간으로 설정
4. 캐시 히트율 모니터링: `openclaw cache stats` (지원 시)
5. Anthropic API의 경우 cache_control 블록을 명시적으로 설정""",
            'source': 'https://github.com/openclaw/openclaw/issues/51873',
        },
        {
            'category': 'openclaw',
            'slug': 'imessage-self-chat-message-loop',
            'title': 'iMessage self-chat creates message loop causing NO_REPLY',
            'symptom': 'iMessage channel enters loop when agent messages itself. Each reply triggers a new incoming message, creating rapid-fire loop. Eventually causes NO_REPLY state.',
            'cause': 'Self-sent messages in iMessage are received as incoming messages. No self-message detection filter exists.',
            'fix': """### iMessage 자체 루프 해결
1. `ignoreSelfMessages: true` 설정 추가
2. 메시지 발신자가 봇 자신인지 확인하는 필터 추가
3. 최소 응답 간격: 같은 대화에 2초 이내 재응답 차단
4. iMessage 대신 다른 채널 사용 검토 (Telegram, Discord 등)""",
            'source': 'https://github.com/openclaw/openclaw/issues/53582',
        },
    ]

    for issue in issues:
        if issue['slug'] in existing:
            continue
        ok = write_solution(
            issue['category'], issue['slug'], issue['title'],
            issue['symptom'], issue['cause'], issue['fix'],
            issue['source'],
            issue.get('tokens_waste', '5,000~15,000'),
            issue.get('tokens_fix', '500'),
        )
        if ok:
            written += 1
            existing.add(issue['slug'])

    return written


def main():
    existing = get_existing_slugs()
    print(f"Existing solutions: {len(existing)}")

    print("\n--- Adding web research solutions ---")
    w1 = add_web_research_solutions(existing)
    print(f"  Written: {w1}")

    print("\n--- Adding GitHub Issues solutions ---")
    w2 = add_github_issues_solutions(existing)
    print(f"  Written: {w2}")

    total = len(get_existing_slugs())
    print(f"\nTotal solutions: {total} ({w1 + w2} new)")

    # Category distribution
    print("\nCategory distribution:")
    for cat_dir in sorted(os.listdir(SOLUTIONS_DIR)):
        cat_path = os.path.join(SOLUTIONS_DIR, cat_dir)
        if os.path.isdir(cat_path):
            count = len([f for f in os.listdir(cat_path) if f.endswith('.md')])
            if count > 0:
                print(f"  {cat_dir}: {count}")


if __name__ == "__main__":
    main()
