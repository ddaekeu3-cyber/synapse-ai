---
layout: solution
title: "Critical memory leak on idle session — 12,396 MB/hour growth rate (v2.1.71, WSL2/Linux)"
category: general
source: https://github.com/anthropics/claude-code/issues/32745
description: "- [x] I have searched existing issues and this hasn't been reported"
---

# Critical memory leak on idle session — 12,396 MB/hour growth rate (v2.1.71, WSL2/Linux)

## 증상
- [x] I have searched [existing issues](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20state%3Aopen%20label%3Abug) and this hasn't been reported yet

## 원인
Agent encountered an unexpected state or unhandled error condition outside the standard error handling path.

## 해결법
```bash
systemd-run --user --scope -p MemoryMax=4G claude
```

---

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/32745
