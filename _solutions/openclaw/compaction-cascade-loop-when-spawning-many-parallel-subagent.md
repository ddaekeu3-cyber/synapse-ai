---
layout: solution
title: "Compaction cascade loop when spawning many parallel subagents via Task tool"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/27794
---

# Compaction cascade loop when spawning many parallel subagents via Task tool

## 증상
Spawning a large number of parallel subagents (10-15+) via the Task tool can trigger an unrecoverable compaction cascade. The parent session's context fills rapidly from subagent return values, triggering compaction. But compaction fires `session_start` internally, which adds overhead back to the context, which triggers more compaction — creating a feedback loop the session cannot escape.

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
https://github.com/anthropics/claude-code/issues/27794
