---
layout: solution
title: "sessions_send from cron/heartbeat context deadlocks on nested lane (maxConcurrent: 1) - regression from PR #45459"
category: telegram
---

# sessions_send from cron/heartbeat context deadlocks on nested lane (maxConcurrent: 1) - regression from PR #45459

## 증상
Regression (worked before, now fails)



## 원인
원본 이슈에서 확인 필요. GitHub Issue #52271 참조.

## 해결법
]
3. Inner embedded agent run enters nested lane (holds slot 1/1)
4. Agent calls sessions_send(targetSession, message)
5. Target session dispatch needs nested lane → blocked (slot occupied by step 3)
6. Caller waits via agent.wait(runId, timeoutMs) for target response
7. Target can never start → caller times out → deadlock

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: telegram
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/52271
