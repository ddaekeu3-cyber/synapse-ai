---
layout: solution
title: "Native heap leak: 1.15B identical 176-byte objects (189GB) in worker process during active tool use"
category: general
source: https://github.com/anthropics/claude-code/issues/36956
---

# Native heap leak: 1.15B identical 176-byte objects (189GB) in worker process during active tool use

## 증상
Claude Code worker process leaks ~189GB of native heap memory over a 10-hour active session. The leak consists of **~1.15 billion identical 176-byte malloc chunks** on the glibc `[heap]`, entirely outside Bun's mimalloc-managed memory.

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
We built an `LD_PRELOAD` guard that caps the leak. **No binary modification needed.**

👉 **https://github.com/dalsoop/claude-code-memory-leak-fix**

```bash
git clone https://github.com/dalsoop/claude-code-memory-leak-fix.git
cd claude-code-memory-leak-fix
make && sudo make install
claude-safe  # drop-in replacement for 'claude'
```

- Below 10GB: zero overhead (passthrough)
- Above 10GB: pools leaked 160-byte objects to stop heap growth
- `kill -USR1 $(pgrep -n claude)` to check stats anytime

Testing confirmed **`free()` is never called** for these objects:
```
[malloc-guard] 160B alloc=17 f

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/36956
