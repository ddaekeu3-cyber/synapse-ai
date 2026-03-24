---
layout: solution
title: "OpenCLaw incorrectly parses natural language messages as structured JSON"
category: telegram
---

# OpenCLaw incorrectly parses natural language messages as structured JSON

## 증상
Behavior bug (incorrect output/state without crash)

에러 메시지:
```shell
### Redacted OpenCLaw Log Snippet

2026-03-22T02:33:32.443+00:00 [gateway] auth token was missing. Generated a new token and saved it to config (gateway.auth.token).
2026-03-22T02:33:32.772+0

## 원인
원본 이슈에서 확인 필요. GitHub Issue #52119 참조.

## 해결법
; newName="d5edbb3056d8 (OpenClaw) (2)"
2026-03-22T02:33:34.638+00:00 [bonjour] gateway hostname conflict resolved; newHostname="openclaw-(2)"
2026-03-22T02:33:35.123+00:00 [embedded] run agent start: runId=a2343d93-93f6-4c15-be56-7100272d4b9c
2026-03-22T02:33:35.124+00:00 [embedded] run agent end: runId=a2343d93-93f6-4c15-be56-7100272d4b9c isError=true error=400 The data couldn’t be read because 

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: telegram
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/52119
