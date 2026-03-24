# Memory Search fails with `fetch failed` when behind proxy (HTTP_PROXY not respected)

## 증상
- **OpenClaw Version**: 2026.3.13 (61d171a)

에러 메시지:
```json
{
  "disabled": true,
  "unavailable": true,
  "error": "fetch failed",
  "warning": "Memory search is unavailable due to an embedding/provider error."
}
```

## 원인
원본 이슈에서 확인 필요. GitHub Issue #53007 참조.

## 해결법
Use `undici`'s built-in `EnvHttpProxyAgent` to enable automatic proxy support:

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: gog
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/53007
