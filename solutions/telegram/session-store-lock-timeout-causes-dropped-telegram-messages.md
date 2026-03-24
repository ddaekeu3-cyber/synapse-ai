# Session store lock timeout causes dropped Telegram messages

## 증상
When processing longer tool chains (e.g. multiple exec/read calls), incoming Telegram messages are silently dropped with the following error:

에러 메시지:
```
[telegram] handler failed: Error: timeout waiting for session store lock: /home/node/.openclaw/agents/main/sessions/sessions.json
```

## 원인
원본 이슈에서 확인 필요. GitHub Issue #49321 참조.

## 해결법
`openclaw gateway restart` clears all locks

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: telegram
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/49321
