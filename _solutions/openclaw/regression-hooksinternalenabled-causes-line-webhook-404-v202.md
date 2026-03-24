---
layout: solution
title: "[regression?] hooks.internal.enabled causes LINE webhook 404 (v2026.3.13)"
category: openclaw
---

# [regression?] hooks.internal.enabled causes LINE webhook 404 (v2026.3.13)

## 증상
Regression (worked before, now fails)

에러 메시지:
```json
   "hooks": {
     "internal": {
       "enabled": true
     }
   }
   ```
4. Restart gateway
5. Test LINE webhook: `curl -X POST http://localhost:18789/line/webhook` → 404
6. Remove the `hook

## 원인
원본 이슈에서 확인 필요. GitHub Issue #52729 참조.

## 해결법
from #31885 (plugin routes before SPA catch-all) is confirmed present. The hooks handler explicitly checks its basePath and returns `false` for `/line/webhook`.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/52729
