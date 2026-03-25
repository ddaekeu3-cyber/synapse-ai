---
layout: solution
title: "sandbox.network.allowedDomains does not work for Node.js processes (DNS resolution blocked)"
category: docker
source: https://github.com/anthropics/claude-code/issues/37782
---

# sandbox.network.allowedDomains does not work for Node.js processes (DNS resolution blocked)

## 증상
When Node.js CLI tools (e.g., tools using `fetch()` or `dns.lookup()`) are executed via the Bash tool, they fail with `ENOTFOUND` DNS errors even when the target domain is listed in `sandbox.network.allowedDomains` **and** the command is in `excludedCommands`. The same domain resolves and connects successfully with `curl` under the identical sandbox configuration.

## 원인
보고된 버그/문제. 카테고리: docker.

## 해결법
Use `dangerouslyDisableSandbox: true` when invoking Node.js-based CLI tools via the Bash tool, or create a PreToolUse hook that automatically injects `updatedInput` with `dangerouslyDisableSandbox: true` for known commands.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37782
