---
layout: solution
title: "Sandbox write/edit tools fail with 'boundary checks failed' despite workspaceAccess: rw"
category: docker
---

# Sandbox write/edit tools fail with 'boundary checks failed' despite workspaceAccess: rw

## 증상
The `write` and `edit` tools fail to create files or directories in the sandbox workspace, reporting "Sandbox boundary checks failed; cannot create directories" even when `agents.defaults.sandbox.workspaceAccess` is correctly set to `rw`.

에러 메시지:
```json
{
  "agents": {
    "defaults": {
      "sandbox": {
        "mode": "all",
        "workspaceAccess": "rw"
      }
    }
  }
}
```

Verified with:
```bash
$ openclaw sandbox explain
workspace

## 원인
원본 이슈에서 확인 필요. GitHub Issue #30513 참조.

## 해결법
The `exec` tool works correctly:
```bash
cat > /workspace/skills/new-skill/SKILL.md << 'EOF'

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: docker
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/30513
