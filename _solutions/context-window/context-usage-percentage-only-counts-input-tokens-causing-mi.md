---
layout: solution
title: "Context usage percentage only counts input tokens, causing misleading 'Context limit reached' at ~20%"
category: context-window
source: https://github.com/anthropics/claude-code/issues/28167
description: "The context usage percentage displayed in both the status line () and the command only accounts for input tokens, while the actual context limit check"
---

# Context usage percentage only counts input tokens, causing misleading 'Context limit reached' at ~20%

## 증상
The context usage percentage displayed in both the **status line** (`used_percentage`) and the **`/context` command** only accounts for **input tokens**, while the actual context limit check considers **total tokens (input + output + cache)**. This creates a confusing situation where users see low usage but hit the context limit.

## 원인
Input exceeded the model's maximum context length, causing truncation or a refusal to process the full request. 카테고리: context-window.

## 해결법
Users can work around this by writing a custom status line script that manually calculates total usage from the individual token fields:

```sh
used=$(echo "$input" | jq -r '
  .context_window |
  if .context_window_size and .context_window_size > 0 then
    (((.input_tokens // 0) + (.output_tokens // 0)
      + (.cache_creation_input_tokens // 0)
      + (.cache_read_input_tokens // 0))
     / .context_window_size * 100 * 10 | floor / 10 | tostring)
  else
    .used_percentage // empty
  end
')
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/28167
