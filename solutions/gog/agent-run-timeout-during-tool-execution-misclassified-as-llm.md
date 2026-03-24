# Agent run timeout during tool execution misclassified as LLM timeout, triggers unnecessary model fallback

## 증상
Agent run timeout during long tool execution (e.g. `process(poll)`) is misclassified as "LLM request timed out", triggering unnecessary model fallback — even though the primary model responded correctly.

에러 메시지:
```
WARN  embedded run timeout: runId=<redacted> sessionId=<redacted> timeoutMs=600000
DEBUG run cleanup: runId=<redacted> sessionId=<redacted> aborted=true timedOut=true
WARN  embedded_run_failover_d

## 원인
원본 이슈에서 확인 필요. GitHub Issue #52147 참조.

## 해결법
Add a `timedOutDuringToolExecution` flag (or refactor to a general `timeoutCause` enum) so tool execution time is exempt from the failover path, consistent with the existing compaction exemption:

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: gog
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/52147
