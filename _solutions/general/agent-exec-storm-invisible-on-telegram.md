---
layout: solution
title: "Agent Exec Storm Invisible on Telegram Messaging Channel"
category: general
tags: agent-behavior, observability, messaging-channel
description: "- 유저가 텔레그램으로 간단한 한 마디 (\"나왔다 보내라\") 전송 - 에이전트가 뒤에서 폭주: find 탐색, 35KB 파일 전체 읽기, exec 20회+, poll 루프 6번 반복 - 이미 tmux 다른 pane에서 같은 스크립트 실행 중인데 확인 없이 중복 실행 -"
---

# 에이전트 exec 폭주가 텔레그램에서 감지 불가능한 문제

## 증상
- 유저가 텔레그램으로 간단한 한 마디 ("나왔다 보내라") 전송
- 에이전트가 뒤에서 폭주: find 탐색, 35KB 파일 전체 읽기, exec 20회+, poll 루프 6번 반복
- 이미 tmux 다른 pane에서 같은 스크립트 실행 중인데 확인 없이 중복 실행
- **텔레그램에는 최종 메시지만 표시** — 유저는 중간 과정을 전혀 모름
- 웹/TUI에서 토큰 R34k(약 34,000 토큰) 소비된 것을 나중에 발견

## 원인

### 직접 원인
1. **텔레그램 = 블랙박스**: 메시징 채널은 최종 응답만 전달, 중간 tool call(exec, read, find)은 유저에게 보이지 않음
2. **경로 무지 → 광범위 탐색**: 에이전트가 파일 위치를 몰라서 `find /` 수준의 탐색 시작
3. **poll 루프 진입**: 결과 대기를 위해 poll 루프에 빠져 타임아웃까지 반복
4. **중복 실행 미확인**: 이미 tmux 4번 pane에서 같은 작업 실행 중인지 체크 안 함

### 근본 원인
- **메시징 채널에는 에이전트 행동의 observability가 없음**
- 웹/TUI에서는 tool call이 실시간으로 보이지만, 텔레그램/슬랙/디스코드에서는 최종 텍스트만 전달
- 에이전트가 자율적으로 긴 작업에 돌입하면 **유저 제어 불가, 중단 불가, 인지 불가**

## 해결법

### 1. 실행 전 확인 원칙 (CLAUDE.md에 추가)

```markdown
# Agent Rules for Messaging Channels

## 실행 전 확인
- exec 3회 이상 필요한 작업은 먼저 유저에게 계획 보고
  예: "파일 3개 수정하고 테스트 실행할게요. 진행할까요?"
- 유저가 OK하면 실행, 아니면 중단

## 단순 요청에 과잉 반응 금지
- "보내라" = 이미 준비된 것을 보내라는 뜻. 처음부터 탐색하라는 뜻이 아님
- 모호하면 확인: "어떤 파일을 보내드릴까요?"
```

### 2. tmux 먼저 확인 (중복 실행 방지)

```bash
# 실행 전 해당 pane에서 이미 돌아가는지 체크
tmux capture-pane -t 4 -p | tail -5
# 프로세스 실행 중이면 → "이미 pane 4에서 실행 중입니다" 보고
# 프로세스 없으면 → 실행 진행
```

CLAUDE.md에 추가:
```markdown
## tmux 작업 전 규칙
실행 전 반드시 `tmux capture-pane -t <pane> -p | tail -5`로 현재 상태 확인.
이미 실행 중이면 중복 실행하지 않고 유저에게 보고.
```

### 3. poll 루프 금지 — background 전환

```markdown
## poll 루프 금지
- 결과 대기를 위한 반복 poll 금지
- background로 돌리고: `command &`
- 나중에 확인: `tmux capture-pane -t <pane> -p`
- poll 대신 1회 확인 → 안 끝났으면 "아직 실행 중" 보고
```

### 4. 행동 로그 파일 (action-log)

```bash
# 에이전트가 수행한 모든 action을 기록
echo "[$(date +%H:%M:%S)] exec: find ~/projects -name '*.py'" >> ~/.openclaw/action-log.md
echo "[$(date +%H:%M:%S)] read: /path/to/file.py (35KB)" >> ~/.openclaw/action-log.md
echo "[$(date +%H:%M:%S)] exec: python3 script.py" >> ~/.openclaw/action-log.md
```

CLAUDE.md에 추가:
```markdown
## Action Logging
모든 exec, read, write 작업을 ~/.openclaw/action-log.md에 기록.
유저가 "뭐 했어?" 물으면 이 파일 참조.
```

### 5. 중간 보고 (메시징 채널용)

```markdown
## 메시징 채널 보고 규칙
텔레그램/슬랙/디스코드에서 작업 시:
1. 긴 작업 시작 전: "X 작업 시작합니다 (예상 30초)"
2. 5회 이상 tool call 시: 중간 상태 보고
3. 에러 발생 시: 즉시 보고 (자동 재시도 3회 후)
4. 완료 시: 결과 + 소비 토큰 보고
```

### 전체 CLAUDE.md 추가 블록

```markdown
# Messaging Channel Safety Rules

1. exec 3회 이상 → 먼저 유저에게 계획 보고
2. tmux 작업 전 → capture-pane으로 현재 상태 확인
3. poll 루프 금지 → background + 1회 확인
4. action-log 기록 → ~/.openclaw/action-log.md
5. 중간 보고 → 긴 작업 시작/진행/완료 보고
6. 단순 요청에 과잉 반응 금지 → 모호하면 확인
```

## 예상 토큰 절약
이 패턴으로 삽질 시: 약 30,000~50,000 토큰 소비 (exec 폭주 + find 탐색 + poll 루프)
규칙 적용 시: 약 2,000 토큰 (확인 → 최소 실행)

## 참고
- 직접 경험 (2026-03-26). OpenClaw v2026.3.23-2, 텔레그램 채널.
- 텔레그램/슬랙/디스코드 등 메시징 채널은 모두 같은 문제 있음
- 웹/TUI에서는 tool call이 실시간 표시되므로 이 문제 없음
- 관련: heartbeat-monitor-false-ok.md (에러 없음 ≠ 정상 패턴)
