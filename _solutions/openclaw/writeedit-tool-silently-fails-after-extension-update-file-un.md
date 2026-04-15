---
layout: solution
title: "Write/Edit tool silently fails after extension update (file unchanged, no error thrown)"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/36084
description: "- Claude Code VSCode Extension (updated"
---

# Write/Edit tool silently fails after extension update (file unchanged, no error thrown)

## 증상
- Claude Code VSCode Extension (updated 2026-03-19)

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Verify every Write/Edit with an immediate grep check:
```bash
grep -c "unique_new_string" target_file
```
If count is 0, the write failed — retry the operation.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/36084
