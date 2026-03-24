# Gemini CLI OAuth fails with 400 Bad Request: loadCodeAssist failed

## 증상
- **OpenClaw Version**: 2026.3.11 (29dc654)

에러 메시지:
```
Gemini CLI OAuth failed
Error: loadCodeAssist failed: 400 Bad Request
```

## 원인
원본 이슈에서 확인 필요. GitHub Issue #44858 참조.

## 해결법
Currently using `kimi-coding/k2p5` as the primary model to avoid dependency on Gemini OAuth.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: gog
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/44858
