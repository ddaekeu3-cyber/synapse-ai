---
layout: solution
title: "Filesystem MCP extension crashes on Windows — built-in Node.js incompatibility"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/27719
---

# Filesystem MCP extension crashes on Windows — built-in Node.js incompatibility

## 증상
The built-in Filesystem MCP extension consistently crashes on Windows when launched via Claude Desktop's built-in Node.js. The server starts, receives the `initialize` message, then silently exits ~1-4 seconds later with no error output on stderr.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Installing `@modelcontextprotocol/server-filesystem` globally and configuring it in `claude_desktop_config.json` with `"command": "node"` (using the system Node.js instead of the built-in one) works correctly. The server name must NOT be `"filesystem"` as Claude Desktop ignores config entries with that name.

```json
"local-fs": {
  "command": "node",
  "args": [
    "C:\Users\<user>\AppData\Roaming\npm\node_modules\@modelcontextprotocol\server-filesystem\dist\index.js",
    "C:\path\to\allowed\directory"
  ]
}
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/27719
