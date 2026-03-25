---
layout: solution
title: "memory_search returns empty results in 2026.3.13 (regression from #29112 fix)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/46671
---

# memory_search returns empty results in 2026.3.13 (regression from #29112 fix)

## 증상
memory_search returns no results for any query on OpenClaw 2026.3.13, despite successful indexing and data existing in SQLite database. This appears to be a regression or incomplete fix of issue #29112 which was reported as closed on 2026-02-28.

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
https://github.com/openclaw/openclaw/issues/46671
