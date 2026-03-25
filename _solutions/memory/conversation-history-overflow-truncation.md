---
layout: solution
title: "Chat history grows until truncated, losing critical early context"
category: memory
source: Perivitta Rajendran - Why Hallucination Happens
---

# Chat history grows until truncated, losing critical early context

## 증상
Long conversations hit context window limit. System auto-truncates older messages. Agent loses crucial earlier instructions, constraints, or decisions.

## 원인
Chat history grows indefinitely until it exceeds context limits. Automatic truncation removes oldest messages regardless of importance.

## 해결법
### 대화 히스토리 오버플로우 해결

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
   - 일반 대화: 오래되면 요약 후 삭제

## 예상 토큰 절약
이 에러로 삽질 시: 약 15,000~40,000 토큰 소비
이 해결법 참조 시: 약 2,000 토큰

## 출처
Perivitta Rajendran - Why Hallucination Happens
