---
layout: solution
title: "PSA: Claude Code v2.1.72/v2.1.73 has confirmed memory leaks — workaround inside"
category: openclaw
source: Reddit r/ClaudeAI https://reddit.com/r/ClaudeAI/comments/1rr65dz/psa_claude_code
---

# PSA: Claude Code v2.1.72/v2.1.73 has confirmed memory leaks — workaround inside

## 증상
If you're experiencing freezes, crashes, or unusually high memory usage on v2.1.72 or v2.1.73 — you're not alone. There are multiple confirmed reports on GitHub today.

The bug: Claude Code v2.1.72 and v2.1.73 have a native memory leak in ArrayBuffers that grow unbounded during sessions:

- \~490 MB/hour on macOS (Apple Silicon) — #33320
- \~980 MB/hour on Linux with heavy subagent/MCP usage — #33

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
Reddit r/ClaudeAI https://reddit.com/r/ClaudeAI/comments/1rr65dz/psa_claude_code_v2172v2173_has_confirmed_memory/
