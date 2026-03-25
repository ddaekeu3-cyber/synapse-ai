---
layout: solution
title: "Accumulated PDF/image Read tool results permanently kill conversations — unrecoverable rate limit loop"
category: rate-limit
source: https://github.com/anthropics/claude-code/issues/30542
---

# Accumulated PDF/image Read tool results permanently kill conversations — unrecoverable rate limit loop

## 증상
- [x] I have searched [existing issues](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20state%3Aopen%20label%3Abug) and this hasn't been reported yet

## 원인
보고된 버그/문제. 카테고리: rate-limit.

## 해결법
is to reduce context, not wait
- Multimodal content is the primary token consumer

This overlaps with **#25805** (rate limit errors don't distinguish usage vs. throughput limits).

#### 5. No recovery path
Once stuck, the user has no in-product way to recover. `/compact` should help but didn't in this case. The only successful recovery required manually editing the JSONL file — something no normal user would know how to do.

---

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/30542
