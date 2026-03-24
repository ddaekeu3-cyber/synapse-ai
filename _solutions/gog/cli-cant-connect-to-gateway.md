---
layout: solution
title: "cli can't connect to gateway"
category: gog
---

# cli can't connect to gateway

## 증상
Behavior bug (incorrect output/state without crash)

에러 메시지:
```shell
error output

🦞 OpenClaw  2026.3.13 (61d171a) — Your .zshrc wishes it could do what I do.

│
gateway connect failed: Error: gateway closed (1000):
◇
[openclaw] Failed to start CLI: Error: gat

## 원인
원본 이슈에서 확인 필요. GitHub Issue #52749 참조.

## 해결법
Use exact command names (for example: canvas.present, canvas.hide, canvas.navigate, canvas.eval, canvas.snapshot, canvas.a2ui.push, canvas.a2ui.pushJSONL, canvas.a2ui.reset). If you need broader restrictions, remove risky command IDs from allowCommands/default workflows and tighten tools.exec policy.
Full report: openclaw security audit
Deep probe: openclaw security audit --deep

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: gog
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/52749
