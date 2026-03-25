---
layout: solution
title: "Misleading error: 'approval-timeout' when exec is blocked by sandbox mode (not approvals system)"
category: tool-failure
source: https://github.com/openclaw/openclaw/issues/26666
---

# Misleading error: 'approval-timeout' when exec is blocked by sandbox mode (not approvals system)

## 증상
When an agent's exec tool call is blocked due to **sandbox mode** (non-main session sandboxed), the error returned is:

## 원인
보고된 버그/문제. 카테고리: tool-failure.

## 해결법
```bash
# Per-session
/elevated full

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/26666
