# Plugin runtime WebSocket race condition crashes gateway on startup

## 증상
Crash (process/app exits or hangs)

에러 메시지:
```
node:events:497
 throw er; // Unhandled 'error' event
 ^

Error: connect ECONNREFUSED 127.0.0.1:18789
 at TCPConnectWrap.afterConnect [as oncomplete] (node:net:1637:16)
Emitted 'error' event on We

## 원인
원본 이슈에서 확인 필요. GitHub Issue #49051 참조.

## 해결법
이 이슈의 해결법은 원본 GitHub Issue를 참조하세요.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: telegram
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/49051
