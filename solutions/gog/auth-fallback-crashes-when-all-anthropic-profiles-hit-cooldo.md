# Auth fallback crashes when all Anthropic profiles hit cooldown simultaneously

## 증상
When all Anthropic auth profiles hit billing cooldown simultaneously, the gateway crashes with an unhandled rejection. The auth selector passes `undefined` as the API key instead of falling through to the configured Gemini fallback.



## 원인
원본 이슈에서 확인 필요. GitHub Issue #47689 참조.

## 해결법
When auth selection for the primary model's provider returns no valid profile, check if model fallbacks are configured and try auth selection for those providers before giving up.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: gog
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/47689
