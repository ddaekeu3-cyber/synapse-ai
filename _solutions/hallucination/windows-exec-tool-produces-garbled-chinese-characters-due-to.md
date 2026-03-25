---
layout: solution
title: "Windows exec tool produces garbled Chinese characters due to hardcoded UTF-8 encoding"
category: hallucination
source: https://github.com/openclaw/openclaw/issues/50519
---

# Windows exec tool produces garbled Chinese characters due to hardcoded UTF-8 encoding

## 증상
Behavior bug (incorrect output/state without crash)

## 원인
보고된 버그/문제. 카테고리: hallucination.

## 해결법
Currently, users must:
1. Avoid using exec tool for commands with Chinese characters
2. Use alternative tools (list_files, read_file, etc.)
3. Use PowerShell with JSON output: `powershell -Command "Get-ChildItem | ConvertTo-Json"`

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50519
