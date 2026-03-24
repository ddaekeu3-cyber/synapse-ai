---
layout: solution
title: "feat: sub-agent templates (pre-defined prompt + model + timeout)"
category: telegram
---

# feat: sub-agent templates (pre-defined prompt + model + timeout)

## 증상
Orchestrator agents that spawn sub-agents with recurring configurations (same prompt, model, timeout) have no way to pre-define reusable sub-agent types. Today, the orchestrator must manually read prompt files, assemble task strings, and pass model/timeout params on every `sessions_spawn` call. This

에러 메시지:
` call. This is repetitive, error-prone, and makes the orchestration logic harder to follow.

## Use Cases

- **Multi-reviewer pipelines:** Spawn N specialized reviewers in parallel, each with a diffe

## 원인
원본 이슈에서 확인 필요. GitHub Issue #50768 참조.

## 해결법
Add a `subagents.templates` config to `agents.list[]`:

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: telegram
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/50768
