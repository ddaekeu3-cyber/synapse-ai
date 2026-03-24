# Slack WebSocket 408 timeout causes unhandled promise rejection + gateway crash

## 증상
The OpenClaw gateway crashes when the Slack Socket Mode WebSocket connection receives a 408 (Request Timeout) response. An unhandled promise rejection is thrown and the Node.js process exits.

에러 메시지:
```
[WARN]  socket-mode:SlackWebSocket:144 A pong wasn't received from the server before the timeout of 5000ms!
[ERROR] socket-mode:SlackWebSocket:149 WebSocket error occurred: Unexpected server respo

## 원인
원본 이슈에서 확인 필요. GitHub Issue #45852 참조.

## 해결법
Wrapping `openclaw-gateway` in a watchdog loop (`while true; do openclaw-gateway; sleep 5; done`) provides auto-restart, but the root cause (unhandled rejection in Slack WebSocket error handler) should be fixed.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/45852
