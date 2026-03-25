#!/usr/bin/env python3
"""Add solutions from HN/Medium web research + GitHub closed issues with solutions."""

import os
import re
import json

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

def write_solution(category, slug, title, symptom, cause, fix, source, tw="5,000~15,000", tf="500"):
    cat_dir = os.path.join(SOLUTIONS_DIR, category)
    os.makedirs(cat_dir, exist_ok=True)
    filepath = os.path.join(cat_dir, f"{slug}.md")
    if os.path.exists(filepath):
        return False
    ct = title.replace('"', "'")
    content = f"""---
layout: solution
title: "{ct}"
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
이 에러로 삽질 시: 약 {tw} 토큰 소비
이 해결법 참조 시: 약 {tf} 토큰

## 출처
{source}
"""
    with open(filepath, 'w') as f:
        f.write(content)
    return True


def add_local_llm_solutions(existing):
    """From MLJourney - debugging common local LLM errors."""
    written = 0
    solutions = [
        {
            'category': 'performance',
            'slug': 'cuda-out-of-memory-oom-llm',
            'title': 'CUDA out of memory (OOM) when running local LLM',
            'symptom': '"CUDA out of memory" or "failed to allocate X bytes" error. System freezes (Windows) or process terminates (Linux). Cannot load or run the model.',
            'cause': 'Model weights exceed available VRAM/RAM. KV cache memory scales with context length and batch size. GPU memory fragmentation from repeated load/unload cycles.',
            'fix': """### OOM 에러 해결

1. **메모리 확인**: `nvidia-smi` (GPU) 또는 `htop` (CPU)로 가용 메모리 확인
2. **양자화 적용**: FP16 → 8-bit (메모리 절반) 또는 4-bit (1/4)
   ```bash
   # llama.cpp 4-bit 양자화
   ./quantize model.gguf model-q4.gguf q4_k_m
   ```
3. **컨텍스트 윈도우 축소**: 8K → 4K로 줄이면 KV 캐시 1/4
4. **레이어 오프로딩**: GPU VRAM + CPU RAM 분할 사용
   ```bash
   ./llama-server -m model.gguf -ngl 20  # 20 레이어만 GPU
   ```
5. **프로세스 재시작**: GPU 메모리 단편화 해소""",
            'source': 'MLJourney - Debugging Common Local LLM Errors',
            'tw': '10,000~30,000',
        },
        {
            'category': 'config',
            'slug': 'model-loading-failure-format-mismatch',
            'title': 'Model loading fails: format mismatch, corruption, or permission denied',
            'symptom': '"Model not found", "permission denied", "Invalid model file", "unknown version", "Unexpected EOF", or "corruption detected" errors when loading local LLM.',
            'cause': 'Incompatible file formats (GGUF vs GGML vs safetensors). Corrupted partial downloads. File permission restrictions or incorrect paths.',
            'fix': """### 모델 로딩 실패 해결

1. **경로 확인**: 절대 경로 사용, 공백은 따옴표로 감싸기
2. **포맷 호환성**:
   - llama.cpp → GGUF 포맷
   - transformers → safetensors 포맷
   - 틀리면 변환 도구 사용
3. **파일 무결성 검증**:
   ```bash
   sha256sum model.gguf  # 공식 체크섬과 비교
   ```
4. **권한 수정**: `chmod +r model.gguf`
5. **재다운로드**: 체크섬 불일치 시 resume 지원 다운로더로 재다운로드""",
            'source': 'MLJourney - Debugging Common Local LLM Errors',
        },
        {
            'category': 'config',
            'slug': 'cuda-gpu-driver-initialization-failed',
            'title': 'CUDA initialization failed / GPU not recognized despite nvidia-smi working',
            'symptom': '"CUDA initialization failed", "No kernel image available for device". GPU not recognized by PyTorch despite nvidia-smi showing the GPU.',
            'cause': 'PyTorch CUDA version mismatches with GPU drivers. Conflicting applications holding GPU locks. Outdated or incompatible GPU drivers.',
            'fix': """### CUDA/GPU 드라이버 문제 해결

1. **버전 호환성 확인**:
   ```bash
   nvcc --version       # 시스템 CUDA
   nvidia-smi           # 드라이버 버전
   python -c "import torch; print(torch.version.cuda)"  # PyTorch CUDA
   ```
2. **드라이버 업데이트**: `sudo apt install nvidia-driver-535`
3. **PyTorch 재설치** (CUDA 버전 맞춰서):
   ```bash
   pip install torch --index-url https://download.pytorch.org/whl/cu121
   ```
4. **GPU 접근 테스트**: `python -c "import torch; print(torch.cuda.is_available())"`
5. **충돌 앱 종료**: 다른 ML 프레임워크, 마이닝 소프트웨어 등
6. **GPU 리셋**: `nvidia-smi --gpu-reset` (모든 GPU 프로세스 종료됨 주의)""",
            'source': 'MLJourney - Debugging Common Local LLM Errors',
        },
        {
            'category': 'performance',
            'slug': 'slow-inference-performance-degradation-llm',
            'title': 'LLM inference too slow or performance degrades over time',
            'symptom': 'Token generation below 20 tok/s (GPU) or 5 tok/s (CPU). Performance starts strong but degrades over time. High latency despite adequate hardware.',
            'cause': 'Suboptimal CPU thread configuration. Thermal throttling from excessive heat (>80-85°C GPU, >90-95°C CPU). GPU memory bandwidth limitations. Background processes consuming resources.',
            'fix': """### 추론 성능 개선

1. **스레드 수 최적화**: 물리 코어 수에 맞춰 테스트
   ```bash
   OMP_NUM_THREADS=8 ./llama-server -m model.gguf
   ```
2. **온도 모니터링**: `nvidia-smi` (GPU), `lm-sensors` (CPU)
   - 80°C 이상이면 쓰로틀링 발생 → 냉각 개선
3. **RAM 업그레이드**: DDR4 3200MHz+ → CPU 바운드 작업 20-40% 개선
4. **백그라운드 앱 종료**: 브라우저, 업데이트, 바이러스 스캐너
5. **병목 프로파일링**: `nvidia-smi dmon` (GPU), `cProfile` (CPU)""",
            'source': 'MLJourney - Debugging Common Local LLM Errors',
        },
        {
            'category': 'hallucination',
            'slug': 'repetitive-degenerate-output-local-llm',
            'title': 'Local LLM produces repetitive loops or degenerate output',
            'symptom': 'Model loops on same phrases or generates nonsense. Output ignores context or contradicts earlier statements. More hallucinations than cloud API versions.',
            'cause': 'Greedy decoding (temperature=0) creating feedback loops. Quantization artifacts corrupting attention patterns. Context window truncation silently cutting information.',
            'fix': """### 반복/퇴화 출력 해결

1. **샘플링 조정**:
   ```yaml
   temperature: 0.7      # 0 대신 0.7+
   top_p: 0.9            # nucleus sampling
   repeat_penalty: 1.1   # 반복 페널티
   ```
2. **양자화 레벨 확인**: 4-bit에서 문제 심하면 8-bit으로 올려 테스트
3. **컨텍스트 한도 확인**: 실제 토큰 수가 설정된 윈도우 내인지 모니터링
4. **프롬프트 개선**: "제공된 컨텍스트만 기반으로 답변" 등 명시적 제약 추가
5. **시스템 프롬프트 강화**: "모르면 모른다고 답하라" 지시 포함""",
            'source': 'MLJourney - Debugging Common Local LLM Errors',
        },
        {
            'category': 'config',
            'slug': 'python-dependency-conflict-llm-setup',
            'title': 'Python dependency conflicts prevent LLM framework from running',
            'symptom': '"Module not found" errors, "Incompatible versions" warnings. LLM framework mysteriously fails despite apparently correct installation.',
            'cause': 'Pip allowing incompatible package versions. Accumulated cruft from previous installations. Version drift in requirements across environments.',
            'fix': """### Python 의존성 충돌 해결

1. **가상환경 생성** (필수):
   ```bash
   python -m venv llm_env
   source llm_env/bin/activate
   ```
2. **고정 버전 사용**: `requirements.txt`에 정확한 버전 명시
   ```
   transformers==4.35.0
   torch==2.1.0
   ```
3. **클린 설치**: 환경 삭제 → 재생성 → requirements.txt로 설치
4. **충돌 확인**: `pip check` 명령으로 버전 비호환 감지
5. **시스템 패키지 활용**: `apt install python3-torch` 등 (버전 충돌 방지)""",
            'source': 'MLJourney - Debugging Common Local LLM Errors',
        },
        {
            'category': 'config',
            'slug': 'network-firewall-blocks-model-download',
            'title': 'Firewall or proxy blocks model downloads from Hugging Face',
            'symptom': '"Connection refused" or timeout during model downloads. Cannot access Hugging Face or GitHub model repositories. Fails in corporate/restricted networks.',
            'cause': 'Firewall blocking model repository domains. Corporate proxies requiring special configuration. Offline environments lacking pre-downloaded models.',
            'fix': """### 네트워크/방화벽 차단 해결

1. **연결 테스트**: `curl -I https://huggingface.co`
2. **프록시 설정**:
   ```bash
   export HTTP_PROXY=http://proxy:8080
   export HTTPS_PROXY=http://proxy:8080
   ```
3. **오프라인 다운로드**: 연결된 환경에서 미리 다운로드
   ```bash
   export HF_HOME=/path/to/models
   huggingface-cli download model-name
   ```
4. **SSL 검사 우회**: 기업 프록시 SSL 인스펙션 → 인증서 설정 추가
5. **미러 사용**: HuggingFace 미러 사이트 활용 (중국: hf-mirror.com)""",
            'source': 'MLJourney - Debugging Common Local LLM Errors',
        },
    ]

    for sol in solutions:
        if sol['slug'] in existing:
            continue
        ok = write_solution(sol['category'], sol['slug'], sol['title'],
            sol['symptom'], sol['cause'], sol['fix'], sol['source'],
            sol.get('tw', '5,000~15,000'), sol.get('tf', '500'))
        if ok:
            written += 1
            existing.add(sol['slug'])
    return written


def add_hn_web_research_solutions(existing):
    """From HN discussions and web search summaries."""
    written = 0
    solutions = [
        {
            'category': 'context-window',
            'slug': 'multi-turn-conversation-39-percent-drop',
            'title': 'Multi-turn conversations cause 39% performance drop vs single-turn',
            'symptom': 'Agent performs well on single-turn tasks but accuracy drops ~39% in multi-turn conversations. o3 model dropped from 98.1% to 64.1% in multi-turn format.',
            'cause': 'Conflicting context accumulates across turns. Earlier turns may contain outdated or contradictory information. The model struggles to resolve conflicts between different parts of conversation history.',
            'fix': """### 멀티턴 성능 저하 해결

1. **컨텍스트 충돌 감지**: 턴 간 모순되는 정보 자동 감지
2. **명시적 상태 관리**: 각 턴 후 현재 상태를 명확히 기록
   ```
   [현재 상태] Python 3.12, FastAPI, PostgreSQL
   [변경됨] DB를 MySQL로 변경 (턴 #5에서 결정)
   ```
3. **주기적 컨텍스트 리프레시**: 10턴마다 현재 상태 요약 + 히스토리 압축
4. **단일 턴 변환**: 복잡한 태스크는 독립적 단일 턴으로 분리""",
            'source': 'HN Discussion - AgenticQA failure modes (2026)',
            'tw': '20,000~60,000',
            'tf': '3,000',
        },
        {
            'category': 'context-window',
            'slug': 'context-window-junk-drawer-problem',
            'title': 'Context window used as "junk drawer" — everything dumped inside',
            'symptom': 'Agent performance degrades because context window is filled with irrelevant information. Important instructions get diluted among noise. Token costs inflate unnecessarily.',
            'cause': 'Teams treating context windows like junk drawers, dumping everything inside without curation. No information discipline applied to what goes into context.',
            'fix': """### 컨텍스트 정리 전략

1. **정보 분류 체계**:
   - 필수: 시스템 프롬프트, 현재 태스크, 핵심 제약조건
   - 선택: 관련 예시, 이전 결정
   - 제외: 이전 실패 시도, 무관한 대화, 중복 정보

2. **동적 컨텍스트 로딩**: 태스크에 필요한 정보만 선택적 로드
   ```
   if task_type == "code_review":
       load(file_content, style_guide)  # 코드 + 스타일만
   elif task_type == "bug_fix":
       load(error_log, relevant_code, test_results)  # 에러 관련만
   ```

3. **토큰 예산 설정**: 컨텍스트 영역별 토큰 한도 지정
4. **정기적 감사**: 컨텍스트에 들어가는 내용 주기적 검토""",
            'source': 'LogRocket Blog - The LLM Context Problem in 2026',
            'tw': '15,000~40,000',
            'tf': '2,000',
        },
        {
            'category': 'context-window',
            'slug': 'llm-summarization-lossy-compression-fails',
            'title': 'LLM-based context summarization loses critical details',
            'symptom': 'Using LLM to compress conversation history appears to work, but agent later fails because specific details referenced by user were lost in summarization.',
            'cause': 'LLM summarization is inherently lossy. Models cannot consistently identify what information will matter later. Compressed histories look complete but miss specific details users reference.',
            'fix': """### 요약 기반 압축 실패 해결

1. **구조화된 추출** (요약 대신):
   ```json
   {
     "decisions": ["REST → GraphQL 전환"],
     "constraints": ["Python 3.12 필수"],
     "pending_questions": ["DB 선택 미결정"],
     "key_values": {"budget": "$500/mo", "deadline": "4/15"}
   }
   ```

2. **핵심 엔티티 보존**: 요약 시 고유명사, 숫자, 코드 스니펫은 원문 유지

3. **하이브리드 접근**:
   - 최근 5턴: 원문 보존
   - 이전 턴: 구조화 추출 + 원문 아카이브 (필요시 검색)

4. **사용자 검증**: 요약 결과를 사용자에게 확인 요청 (중요 대화 시)""",
            'source': 'LogRocket Blog - The LLM Context Problem in 2026',
        },
        {
            'category': 'loop-stuck',
            'slug': 'recursive-loop-context-exceeds-then-retries',
            'title': 'Agent enters recursive loop: exceeds context → error → retry → same error',
            'symptom': 'Agent constructs context that exceeds token limits, causing an error. The problematic span remains in session. Agent runs again on same data, hits same limit, loops forever.',
            'cause': 'Failed context that exceeded limits remains in session state. No cleanup mechanism removes the problematic data. Each retry reconstructs the same oversized context.',
            'fix': """### 컨텍스트 초과 재귀 루프 해결

1. **실패 시 컨텍스트 정리**:
   ```python
   try:
       response = llm.call(context)
   except ContextTooLongError:
       context = truncate_oldest(context, target=0.8 * max_tokens)
       # 실패한 데이터 제거 후 재시도
   ```

2. **사전 토큰 카운팅**: 호출 전 토큰 수 검증
   ```python
   if count_tokens(context) > max_tokens * 0.9:
       context = compress(context)
   ```

3. **최대 재시도 제한**: 같은 에러 3회 → 중단 + 보고
4. **세션 상태 리셋**: 루프 감지 시 문제 세션 데이터 삭제""",
            'source': 'Arize AI - Managing Memory in AI Agents',
            'tw': '30,000~100,000',
            'tf': '2,000',
        },
        # From web search summaries - token cost articles
        {
            'category': 'token-cost',
            'slug': 'ai-coding-agent-wastes-80-percent-finding',
            'title': 'AI coding agent wastes 80% of tokens on orientation, not problem-solving',
            'symptom': 'AI coding agent spends most of its token budget exploring the codebase rather than actually solving the problem. Token costs are 5x higher than expected for simple tasks.',
            'cause': 'Agent does not have a map of the codebase. It spends tokens reading files, searching for definitions, and understanding structure before it can start actual work.',
            'fix': """### 코드 에이전트 토큰 낭비 80% 줄이기

1. **코드 지식 그래프 구축**:
   - 파일 구조, 함수/클래스 정의, import 관계를 사전 인덱싱
   - 에이전트에게 "지도"를 제공하여 탐색 불필요

2. **컨텍스트 압축**:
   - 전체 파일 대신 관련 함수/클래스만 주입
   - 토큰 40-95% 절약 가능

3. **프로젝트 요약 파일**:
   ```markdown
   # Project Map
   - src/api/ → REST endpoints (auth, users, posts)
   - src/db/ → Database models (SQLAlchemy)
   - src/services/ → Business logic
   ```

4. **CLAUDE.md / AGENTS.md 활용**: 프로젝트 구조를 사전 문서화""",
            'source': 'Medium - Jake Nesler (Context Compression, 2026)',
            'tw': '50,000~200,000',
            'tf': '5,000',
        },
        {
            'category': 'token-cost',
            'slug': 'deterministic-logic-burns-expensive-tokens',
            'title': 'Agent uses expensive LLM tokens for tasks that should be deterministic',
            'symptom': 'Agent uses LLM API calls for simple deterministic tasks like email validation, date parsing, string formatting. Costs accumulate for tasks that could be done for free.',
            'cause': 'No routing logic to separate deterministic tasks from tasks requiring LLM reasoning. Everything goes through the expensive model.',
            'fix': """### 결정론적 작업에 토큰 낭비 방지

1. **태스크 분류기 구현**:
   ```python
   if is_deterministic(task):
       result = run_script(task)  # $0, 마이크로초
   else:
       result = llm.call(task)    # $$, 초 단위
   ```

2. **결정론적으로 할 수 있는 것들**:
   - 이메일 검증 → regex
   - 날짜 파싱 → dateutil
   - JSON 변환 → json.loads()
   - 파일 읽기/쓰기 → 직접 I/O
   - 수학 계산 → Python eval

3. **도구 우선 원칙**: LLM에게 "가능하면 도구를 먼저 사용하라" 지시
4. **비용 태깅**: 각 작업에 토큰 비용 로깅 → 비싼 결정론적 작업 식별""",
            'source': 'Medium - Alex Efimenko (7 Ways to Reduce AI Token Consumption)',
        },
        {
            'category': 'token-cost',
            'slug': 'agent-team-token-cost-multiplier',
            'title': 'Multi-agent teams multiply token costs exponentially',
            'symptom': 'Running multiple agents in a team (planner + executor + reviewer) causes token costs to grow much faster than expected. 3 agents ≠ 3x cost, often 10-20x.',
            'cause': 'Each agent receives full context from other agents. Communication overhead between agents adds tokens. Planner output becomes executor input becomes reviewer input, each time growing.',
            'fix': """### 멀티 에이전트 비용 폭발 방지

1. **통신 프로토콜 최소화**:
   - 에이전트 간 전체 출력 대신 구조화된 요약만 전달
   ```json
   {"action": "fix_bug", "file": "api.py", "line": 42, "fix": "add null check"}
   ```

2. **에이전트 수 최소화**: 정말 필요한 역할만 분리
3. **단계별 실행**: 동시 실행 대신 순차 파이프라인 (불필요한 중복 방지)
4. **공유 컨텍스트 풀**: 중복 없이 공유 메모리에서 읽기
5. **비용 한도 설정**: 팀 전체 토큰 예산 설정 + 초과 시 중단""",
            'source': 'Medium - Markus Sandelin (AI Agent Teams Burning Money)',
            'tw': '100,000~500,000',
            'tf': '10,000',
        },
        # From HN - agent reliability
        {
            'category': 'general',
            'slug': 'agent-breaks-rules-under-pressure',
            'title': 'AI agent breaks safety rules when under operational pressure',
            'symptom': 'Agent follows safety guidelines in normal conditions but starts cutting corners or violating rules when faced with tight deadlines, complex requirements, or repeated failures.',
            'cause': 'Safety instructions compete with task completion goals in the model\'s objective. Under pressure (many retries, complex task), the model prioritizes task completion over safety constraints.',
            'fix': """### 에이전트 안전 규칙 위반 방지

1. **하드코딩된 제약**: 중요한 규칙은 프롬프트가 아닌 코드로 강제
   ```python
   # POST 요청은 코드 레벨에서 사람 승인 필수
   if method == "POST" and not human_approved:
       raise RequiresApproval("POST requires human approval")
   ```

2. **규칙 우선순위 명시**:
   ```
   안전 규칙은 어떤 상황에서도 예외 없이 적용됩니다.
   태스크 완료보다 안전 규칙이 항상 우선합니다.
   규칙을 지킬 수 없으면 태스크를 중단하고 보고하세요.
   ```

3. **감사 로그**: 모든 에이전트 행동을 기록하여 규칙 위반 사후 감지
4. **레드팀 테스트**: 정기적으로 압박 상황에서 규칙 준수 테스트""",
            'source': 'Hacker News Discussion - AI agents break rules under pressure (2026)',
        },
    ]

    for sol in solutions:
        if sol['slug'] in existing:
            continue
        ok = write_solution(sol['category'], sol['slug'], sol['title'],
            sol['symptom'], sol['cause'], sol['fix'], sol['source'],
            sol.get('tw', '5,000~15,000'), sol.get('tf', '500'))
        if ok:
            written += 1
            existing.add(sol['slug'])
    return written


def add_github_closed_issues(existing):
    """Process 87 closed GitHub issues that mention fixes."""
    written = 0

    with open('/tmp/closed_issues.json') as f:
        issues = json.load(f)

    for issue in issues:
        title = issue.get('title', '')
        body = issue.get('body', '') or ''
        url = issue.get('url', '')
        number = issue.get('number', 0)

        # Skip Chinese-only titles
        if re.search(r'[\u4e00-\u9fff]', title) and not re.search(r'[a-zA-Z]{5,}', title):
            continue

        clean_title = re.sub(r'\[Bug\]:?\s*', '', title, flags=re.IGNORECASE).strip()
        if not clean_title or len(clean_title) < 10:
            continue

        slug = slugify(clean_title)
        if not slug or len(slug) < 5 or slug in existing:
            continue

        # Extract fix from body
        fix_text = ""
        fix_patterns = [
            r'(?:fix|solution|workaround|resolved|solved)[:\s]*\n([\s\S]{30,600}?)(?:\n\n\n|\n#{1,3}\s|$)',
            r'(?:I fixed|The fix|To fix|Fixed by)[:\s]*([\s\S]{30,600}?)(?:\n\n\n|\n#{1,3}\s|$)',
            r'(?:Steps to reproduce and fix|Resolution)[:\s]*\n([\s\S]{30,600}?)(?:\n\n\n|\n#{1,3}\s|$)',
        ]
        for p in fix_patterns:
            m = re.search(p, body, re.IGNORECASE)
            if m:
                fix_text = m.group(1).strip()[:500]
                break

        if not fix_text or len(fix_text) < 30:
            continue  # Skip if no real fix found

        # Extract symptom
        symptom = ""
        for line in body.split('\n'):
            line = line.strip()
            if line and not line.startswith('#') and not line.startswith('```') and len(line) > 30:
                symptom = line[:300]
                break

        if not symptom:
            symptom = clean_title

        # Categorize
        text = f"{title} {body}".lower()
        category = 'openclaw'
        if any(w in text for w in ['telegram', 'tg bot']):
            category = 'telegram'
        elif any(w in text for w in ['docker', 'container', 'podman']):
            category = 'docker'
        elif any(w in text for w in ['rate limit', '429', 'throttl']):
            category = 'rate-limit'
        elif any(w in text for w in ['oauth', 'auth', 'credential', 'api key', '401', '403']):
            category = 'auth'
        elif any(w in text for w in ['memory', 'forget', 'index']):
            category = 'memory'
        elif any(w in text for w in ['loop', 'stuck', 'crash loop', 'restart']):
            category = 'loop-stuck'
        elif any(w in text for w in ['token', 'cost', 'usage']):
            category = 'token-cost'
        elif any(w in text for w in ['timeout', 'slow', 'performance', 'latency']):
            category = 'performance'
        elif any(w in text for w in ['config', 'setup', 'env', 'setting']):
            category = 'config'

        ok = write_solution(category, slug, clean_title, symptom,
            f"GitHub Issue #{number}에서 보고된 버그. 해결법이 이슈에 포함됨.",
            fix_text, url)
        if ok:
            written += 1
            existing.add(slug)

    return written


def main():
    existing = get_existing_slugs()
    print(f"Existing solutions: {len(existing)}")

    print("\n--- Local LLM debugging solutions ---")
    w1 = add_local_llm_solutions(existing)
    print(f"  Written: {w1}")

    print("\n--- HN/Web research solutions ---")
    w2 = add_hn_web_research_solutions(existing)
    print(f"  Written: {w2}")

    print("\n--- GitHub closed issues with fixes ---")
    w3 = add_github_closed_issues(existing)
    print(f"  Written: {w3}")

    total = len(get_existing_slugs())
    print(f"\nTotal: {total} ({w1+w2+w3} new)")

    print("\nCategory distribution:")
    for cat_dir in sorted(os.listdir(SOLUTIONS_DIR)):
        cat_path = os.path.join(SOLUTIONS_DIR, cat_dir)
        if os.path.isdir(cat_path):
            count = len([f for f in os.listdir(cat_path) if f.endswith('.md')])
            if count > 0:
                print(f"  {cat_dir}: {count}")


if __name__ == "__main__":
    main()
