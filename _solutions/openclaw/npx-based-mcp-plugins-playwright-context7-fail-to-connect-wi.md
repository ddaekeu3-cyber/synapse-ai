---
layout: solution
title: "npx-based MCP plugins (Playwright, Context7) fail to connect with concurrent sessions"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/34891
---

# npx-based MCP plugins (Playwright, Context7) fail to connect with concurrent sessions

## 증상
MCP plugins that use `npx @pkg@latest` (stdio transport) consistently fail to connect when multiple Claude Code sessions are open concurrently. HTTP-based plugins (Supabase, Sentry) connect fine.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Pin packages locally as devDependencies and use `.mcp.json` with direct `node` execution:

```bash
pnpm add -D @playwright/mcp @upstash/context7-mcp
```

```json
{
  "mcpServers": {
    "playwright": {
      "command": "node",
      "args": ["node_modules/@playwright/mcp/cli.js"]
    },
    "context7": {
      "command": "node",
      "args": ["node_modules/@upstash/context7-mcp/dist/index.js"]
    }
  }
}
```

This eliminates registry lookups entirely — startup goes from ~5s to ~100ms.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/34891
