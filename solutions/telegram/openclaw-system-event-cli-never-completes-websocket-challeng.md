# `openclaw system event` CLI never completes WebSocket challenge-response handshake (macOS, 2026.3.13)

## 증상
Defect / Missing implementation (feature never worked as expected)

에러 메시지:
```
Running node v24.12.0 (npm v11.9.0)
🦞 OpenClaw 2026.3.13 (61d171a)
...
gateway connect failed: Error: gateway closed (1000):
Error: gateway closed (1000 normal closure): no close reason
Gateway ta

## 원인
원본 이슈에서 확인 필요. GitHub Issue #52837 참조.

## 해결법
is to rely solely on `cron` jobs (which run inside the Gateway and are unaffected).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: telegram
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/52837
