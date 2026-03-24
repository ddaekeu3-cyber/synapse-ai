# [Windows] Gateway exits silently without crash log

## 증상
Regression (worked before, now fails)

에러 메시지:
```shell
Timeline (2026-03-19 22:22-22:23 CST):
22:22:45.108 - WebSocket handshake timeout
22:22:45.110 - gateway connect failed
22:22:45.117 - Error: gateway closed (1000): no close reason
22:22:48 -

## 원인
원본 이슈에서 확인 필요. GitHub Issue #50472 참조.

## 해결법
1. Intercept process.exit() and log exit code
2. Add global uncaughtException/unhandledRejection handlers that log before exit
3. Log memory usage before exit if possible
4. Add watchdog to capture and log unexpected termination

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/50472
