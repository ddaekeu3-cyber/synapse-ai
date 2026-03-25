---
layout: solution
title: "TDZ crash on ANTHROPIC_MODEL_ALIASES in bundled dist chunks (2026.3.12 regression)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/45006
---

# TDZ crash on ANTHROPIC_MODEL_ALIASES in bundled dist chunks (2026.3.12 regression)

## 증상
After upgrading from 2026.3.11 to 2026.3.12, `ANTHROPIC_MODEL_ALIASES` throws a `ReferenceError` (TDZ / temporal dead zone) in several bundled dist chunks. The constant is defined at module scope in `src/agents/model-selection.ts`, but the Rollup code-splitting in 2026.3.12 reorders the emitted chunks so that `normalizeAnthropicModelId()` executes before `ANTHROPIC_MODEL_ALIASES` is initialized in

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
Wrap the access in a try/catch in each affected dist chunk:

```js
// Before (crashes with ReferenceError):
return ANTHROPIC_MODEL_ALIASES[trimmed.toLowerCase()] ?? trimmed;

// After (graceful fallback):
try { return ANTHROPIC_MODEL_ALIASES[trimmed.toLowerCase()] ?? trimmed; } catch(e) { return trimmed; }
```

This needed to be applied to ~27 bundle files. A proper fix would be to either:
- Move `ANTHROPIC_MODEL_ALIASES` into a shared chunk that's guaranteed to initialize first
- Use a lazy getter / function instead of a top-level const
- Fix the Rollup chunk ordering to respect the dependenc

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/45006
