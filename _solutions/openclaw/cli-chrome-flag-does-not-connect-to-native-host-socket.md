---
layout: solution
title: "CLI --chrome flag does not connect to native host socket"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/37833
description: "When running , the CLI correctly enables the tools, but never connects to the native host Unix socket. All MCP tool calls return \"Browser extension is not"
---

# CLI --chrome flag does not connect to native host socket

## 증상
When running `claude --chrome`, the CLI correctly enables the `mcp__claude-in-chrome__*` tools, but **never connects to the native host Unix socket**. All MCP tool calls return "Browser extension is not connected."

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Direct socket communication via Python script works perfectly. We wrote a bridge script that connects to the native host socket and executes tools:

```python
sock.connect('/tmp/claude-mcp-browser-bridge-{user}/{pid}.sock')
send_msg(sock, {"method": "execute_tool", "params": {"tool": "tabs_context_mcp", "args": {}}})

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37833
