---
layout: solution
title: "Gateway RPC timeout causes intermittent `openclaw cron list` failures (1000 normal closure)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/46769
---

# Gateway RPC timeout causes intermittent `openclaw cron list` failures (1000 normal closure)

## 증상
- **OpenClaw Version**: 2026.3.13 (61d171a)

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Created a retry wrapper script that achieves 100% success rate:

```bash
#!/bin/bash
MAX_RETRIES=3
RETRY_DELAY=0.5

for i in $(seq 1 $MAX_RETRIES); do
    if openclaw cron list "$@" 2>&1; then
        exit 0
    fi
    if [ $i -lt $MAX_RETRIES ]; then
        sleep $RETRY_DELAY
    fi
done

echo "❌ Failed after $MAX_RETRIES attempts" >&2
exit 1
```

Test results: 10/10 successful attempts with retry logic.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/46769
