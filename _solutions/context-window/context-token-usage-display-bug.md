---
layout: solution
title: "Context Token Usage Display Bug"
category: context-window
source: https://github.com/openclaw/openclaw/issues/45358
description: "Component: Model configuration / Session cost"
---

# Context Token Usage Display Bug

## 증상
**Component:** Model configuration / Session cost tracking

## 원인
Input exceeded the model's maximum context length, causing truncation or a refusal to process the full request. 카테고리: context-window.

## 해결법
Runtime patch that forces `include_usage: true` in the pi-ai library:

```bash
#!/usr/bin/env bash
python3 - <<'PY'
from pathlib import Path
p = Path.home() / ".npm-global/lib/node_modules/openclaw/node_modules/@mariozechner/pi-ai/dist/providers/openai-completions.js"
s = p.read_text()

old = """    if (compat.supportsUsageInStreaming !== false) {
        params.stream_options = { include_usage: true };
    }"""

new = """    params.stream_options = { include_usage: true };"""

if old in s:
    s = s.replace(old, new, 1)
    p.write_text(s)
    print("patched")
elif new in s:
    print("alread

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/45358
