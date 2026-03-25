---
layout: solution
title: "Usage dashboard undercounts tokens/cost — reset archive transcripts are ignored"
category: token-cost
source: https://github.com/openclaw/openclaw/issues/40870
---

# Usage dashboard undercounts tokens/cost — reset archive transcripts are ignored

## 증상
The Usage page in the Control UI (and `loadCostUsageSummary` / `discoverAllSessions` in `session-cost-usage.ts`) significantly undercounts token usage and cost because it only scans files ending in `.jsonl`, ignoring reset archive transcripts named `*.jsonl.reset.<timestamp>`.

## 원인
보고된 버그/문제. 카테고리: token-cost.

## 해결법
PR incoming: match both `.jsonl` and `.jsonl.reset.<timestamp>` patterns when scanning session directories.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/40870
