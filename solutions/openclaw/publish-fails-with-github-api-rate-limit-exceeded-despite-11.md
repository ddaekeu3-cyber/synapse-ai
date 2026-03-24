# Publish fails with "GitHub API rate limit exceeded" despite 119/120 remaining

## 증상
Both CLI (`clawhub publish`) and web UI (clawhub.ai/upload) fail with:

에러 메시지:
`
4. Fails with rate limit error

Same error on web UI at clawhub.ai/upload after filling all fields and clicking Publish.

## Expected

Publish should succeed — 119/120 remaining is clearly not rate 

## 원인
원본 이슈에서 확인 필요. GitHub Issue #1135 참조.

## 해결법
이 이슈의 해결법은 원본 GitHub Issue를 참조하세요.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/clawhub/issues/1135
