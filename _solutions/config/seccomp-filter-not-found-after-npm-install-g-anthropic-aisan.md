---
layout: solution
title: "seccomp filter not found after npm install -g @anthropic-ai/sandbox-runtime"
category: config
source: https://github.com/anthropics/claude-code/issues/37916
description: "reports \"seccomp filter: not installed\" even after following the suggested install command (). The files are on disk but Claude Code never finds them,"
---

# seccomp filter not found after npm install -g @anthropic-ai/sandbox-runtime

## 증상
`/sandbox` reports "seccomp filter: not installed" even after following the suggested install command (`npm install -g @anthropic-ai/sandbox-runtime`). The files are on disk but Claude Code never finds them, requiring a manual settings patch.

## 원인
Environment variable, configuration file, or initialization parameter missing, malformed, or incorrectly scoped.

## 해결법
Manually add explicit paths to `~/.claude/settings.local.json`:

```json
{
  "sandbox": {
    "seccomp": {
      "bpfPath": "<npm-global-prefix>/lib/node_modules/@anthropic-ai/sandbox-runtime/vendor/seccomp/x64/unix-block.bpf",
      "applyPath": "<npm-global-prefix>/lib/node_modules/@anthropic-ai/sandbox-runtime/vendor/seccomp/x64/apply-seccomp"
    }
  }
}
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37916
