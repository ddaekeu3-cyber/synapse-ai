---
layout: solution
title: "Max 20x: Usage meter climbing abnormally fast since ~March 23 — 1-2% per simple message exchange"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/38357
---

# Max 20x: Usage meter climbing abnormally fast since ~March 23 — 1-2% per simple message exchange

## 증상
Since approximately March 23, my Max 20x ($200/mo) plan usage meter is climbing at roughly 5-10x the expected rate. Workloads that previously consumed negligible budget are now burning through the 5-hour window rapidly. This is a sudden change — I've been a heavy Claude Code user for months (multi-agent teams, 50+ concurrent sessions, 100M+ token days) with no usage issues until now.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
1. OpenClaw 최신 버전으로 업데이트: `npm update -g openclaw`
2. Gateway 재시작: `openclaw gateway restart`
3. 설정 파일 확인: `~/.openclaw/config.yaml`
4. 로그 확인: `openclaw logs --tail 50`
5. 원본 GitHub Issue에서 패치 확인

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38357
