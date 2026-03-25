---
layout: solution
title: "Request: Add Chinese-friendly tokenizer support for qmd memory backend"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/45357
---

# Request: Add Chinese-friendly tokenizer support for qmd memory backend

## 증상
I'm using OpenClaw with the qmd memory backend and experiencing issues with Chinese text search. The current SQLite FTS5 configuration uses `tokenize='porter unicode61'`, which has limited support for Chinese language.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Adding English keywords to Chinese content, but this is not ideal for Chinese-speaking users.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/45357
