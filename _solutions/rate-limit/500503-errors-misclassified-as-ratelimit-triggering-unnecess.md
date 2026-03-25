---
layout: solution
title: "500/503 errors misclassified as rate_limit, triggering unnecessary cooldowns"
category: rate-limit
source: https://github.com/openclaw/openclaw/issues/22294
---

# 500/503 errors misclassified as rate_limit, triggering unnecessary cooldowns

## 증상
OpenClaw gateway classifies Gemini 500 (InternalServerError) and 503 (ServiceUnavailable) responses as `rate_limit` errors, which triggers the exponential cooldown mechanism (1min → 5min → 25min → 60min cap). This effectively takes the agent offline even when API usage is well below rate limits.

## 원인
보고된 버그/문제. 카테고리: rate-limit.

## 해결법
Manually clear cooldowns in `auth-profiles.json`:
```bash
python3 -c "
import json
with open('auth-profiles.json') as f:
    data = json.load(f)
for p in data.get('profiles', []):
    for key in ['cooldownUntil', 'errorCount', 'failureCounts', 'lastFailureAt']:
        if key in p:
            del p[key]
with open('auth-profiles.json', 'w') as f:
    json.dump(data, f, indent=2)
"
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/22294
