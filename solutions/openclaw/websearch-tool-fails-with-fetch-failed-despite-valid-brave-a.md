# web_search tool fails with 'fetch failed' despite valid Brave API key

## 증상
- OpenClaw version: 2026.3.13

에러 메시지:
```
2026-03-22T17:35:39.521+08:00 [tools] web_search failed: fetch failed
```

## 원인
원본 이슈에서 확인 필요. GitHub Issue #52177 참조.

## 해결법
**
Direct curl calls work:
```bash
curl --proxy $OPENCLAW_WEB_PROXY \
  -H "X-Subscription-Token: $API_KEY" \
  "https://api.search.brave.com/res/v1/web/search?q=test&count=1"
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/52177
