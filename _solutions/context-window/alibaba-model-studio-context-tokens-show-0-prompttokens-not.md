---
layout: solution
title: "Alibaba Model Studio: Context tokens show 0 (prompt_tokens not recognized)"
category: context-window
source: https://github.com/openclaw/openclaw/issues/52981
description: "When using Alibaba Model Studio models (e.g., , ), OpenClaw displays 0 context tokens used even during active conversations. The context window size shows"
---

# Alibaba Model Studio: Context tokens show 0 (prompt_tokens not recognized)

## 증상
When using Alibaba Model Studio models (e.g., `modelstudio/qwen3.5-plus`, `modelstudio/glm-5`), OpenClaw displays **0 context tokens used** even during active conversations. The context window size shows correctly, but "used" is always 0.

## 원인
Input exceeded the model's maximum context length, causing truncation or a refusal to process the full request. 카테고리: context-window.

## 해결법
The `normalizeUsage()` function (lines 9750-9770) **already handles this**:

```javascript
const rawInput = asFiniteNumber(raw.input ?? raw.inputTokens ?? raw.input_tokens ?? raw.promptTokens ?? raw.prompt_tokens);
const output = asFiniteNumber(raw.output ?? raw.outputTokens ?? raw.output_tokens ?? raw.completionTokens ?? raw.completion_tokens);
```

The fix is to call `normalizeUsage()` before building the usage object.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/52981
