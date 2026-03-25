---
layout: solution
title: "OAuth Pro subscription breaks when ANTHROPIC_AUTH_TOKEN/ANTHROPIC_BASE_URL set in env — users workaround via OpenRouter"
category: auth
source: https://github.com/anthropics/claude-code/issues/33330
---

# OAuth Pro subscription breaks when ANTHROPIC_AUTH_TOKEN/ANTHROPIC_BASE_URL set in env — users workaround via OpenRouter

## 증상
Claude Code with Pro subscription OAuth fails with **401 Invalid bearer token** when environment variables \`ANTHROPIC_AUTH_TOKEN\` or \`ANTHROPIC_BASE_URL\` are set in the active shell session — even after a successful \`/login\`.

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
\`\`\`bash
unset ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN
claude
\`\`\`

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/33330
