---
layout: solution
title: "Agent forgets critical context after session restart (not persisted to file)"
category: memory
tags: [context, session, persistence, memory, workspace-file]
description: "New session starts and agent has no memory of information clearly mentioned in the previous session. Fix: always persist critical context to a workspace file before session ends."
---

## 증상

새 세션을 시작하면 에이전트가 이전 세션에서 분명히 언급되었던 정보를 전혀 모른다.

**실제 사례:**
- 사용자가 `6번 오마클`이라는 별명으로 특정 에이전트를 지칭
- 해당 에이전트는 이전 대화에서 해당 별명을 사용했음
- 그러나 새 세션에서는 "6번 오마클이 뭔가요?"라고 되물음

---

## 원인

에이전트의 기억은 **세션 내 컨텍스트 윈도우**에만 존재한다. 파일에 기록되지 않은 정보는 세션이 종료되는 순간 사라진다.

```
세션 A: "6번 오마클" 언급 → 컨텍스트 윈도우에 존재
세션 종료 → 컨텍스트 윈도우 초기화
세션 B: "6번 오마클"? → 파일에 없으므로 모름
```

**저장된 적 없었던 이유:**
- `MEMORY.md`, `TOOLS.md`, `AGENTS.md`, `CLAUDE.md` 등 어떤 워크스페이스 파일에도 기록하지 않음
- 구두로만 오갔던 컨텍스트는 세션 사이를 건너지 못함

---

## 해결법

### 핵심 원칙

> **"파일에 없으면 없는 것이다."**
>
> 에이전트에게 중요한 정보는 반드시 워크스페이스 파일에 기록해야 세션 간 유지된다.

### 실천 방법

**1. MEMORY.md에 컨텍스트 등록**
```markdown
# 에이전트/팀 별명
- 6번 오마클: 워크스페이스 0.5번 페인에서 실행 중인 oh-my-claudecode 인스턴스
- 한사장: OpenClaw 텔레그램 봇 (@CEO_Han_bot)
```

**2. CLAUDE.md에 지속 참조가 필요한 규칙 추가**
```markdown
## 팀 구성
- 현재 활성 에이전트: 1번(메인), 2번(synapse-ai), 6번(BBQ 조사 등 외부 리서치)
```

**3. 세션 시작 시 파일 읽기 습관화**
에이전트가 새 세션을 시작할 때 `MEMORY.md`를 먼저 읽도록 프롬프트 또는 훅 설정.

---

## 예방 체크리스트

- [ ] 대화 중 새로운 별명/역할/규칙이 정해지면 즉시 파일에 기록
- [ ] 중요한 결정(모델 변경, 팀 구성, 프로젝트 방향)은 `MEMORY.md` 또는 `CLAUDE.md`에 저장
- [ ] 세션 압축(compaction) 전에 중요 컨텍스트가 파일에 있는지 확인
- [ ] 구두로 오간 중요 정보를 `/oh-my-claudecode:note`로 즉시 저장

---

## 참고

- 관련 솔루션: [Telegram 에이전트 세션 분열](../telegram/telegram-session-split-dual-response.md)
- oh-my-claudecode `/note` 스킬: 현재 대화의 중요 내용을 `notepad.md`에 즉시 저장
- 컨텍스트 윈도우 ≠ 영구 메모리 — 이 구분을 항상 인지할 것
