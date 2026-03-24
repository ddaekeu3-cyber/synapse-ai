# Timeout-driven auth rotation/cooldown on timeouts causes premature provider fallback (proposal: retry/backoff same profile)

## 증상
In the embedded runner’s **auth-profile failover loop** (used by providers that support `auth.profiles`), a request timeout is currently treated as a strong signal to rotate/cool down the current profile. This can cascade into **“no available auth profile”** and then into **model/provider fallback**

에러 메시지:
```
Profile openai-codex:default timed out (possible rate limit). Trying next account...
No available auth profile for openai-codex (all in cooldown or unavailable).
... provider=openai model=gpt-5.2 

## 원인
원본 이슈에서 확인 필요. GitHub Issue #23317 참조.

## 해결법
#### Minimal (recommended first step)

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/23317
