# Feature request: Circuit breaker for consecutive LLM timeouts

## 증상
When an LLM request times out, OpenClaw retries on the next heartbeat/message. If the root cause persists (e.g., context too large, provider degradation), the system retries indefinitely — compounding the failure. We observed 233 timeouts across 13 agents over 10 days, with one agent stuck in a retr



## 원인
원본 이슈에서 확인 필요. GitHub Issue #45389 참조.

## 해결법
Add a configurable circuit breaker:

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/45389
