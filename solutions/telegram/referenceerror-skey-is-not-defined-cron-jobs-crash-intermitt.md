# ReferenceError: _skey is not defined — cron jobs crash intermittently

## 증상
Cron-triggered sessions (both `agentTurn` isolated and `systemEvent` main) intermittently crash with:

에러 메시지:
```
ReferenceError: _skey is not defined
```

## 원인
원본 이슈에서 확인 필요. GitHub Issue #49054 참조.

## 해결법
None known. Setting `delivery: "none"` does not prevent the error.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: telegram
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/49054
