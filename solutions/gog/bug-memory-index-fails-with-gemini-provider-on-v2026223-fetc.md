# Bug: memory index fails with Gemini provider on v2026.2.23 (fetch failed)

## 증상
`openclaw memory index` fails with `fetch failed` when using Gemini as embedding provider on OpenClaw v2026.2.23.

에러 메시지:
`fetch failed`

## 원인
원본 이슈에서 확인 필요. GitHub Issue #26069 참조.

## 해결법
Use local embeddings (`memorySearch.provider=local`) or an OpenAI-compatible embedding endpoint until fixed.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: gog
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/26069
