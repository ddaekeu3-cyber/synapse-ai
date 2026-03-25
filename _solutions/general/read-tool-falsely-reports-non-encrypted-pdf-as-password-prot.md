---
layout: solution
title: "Read tool falsely reports non-encrypted PDF as password-protected"
category: general
source: https://github.com/anthropics/claude-code/issues/38530
---

# Read tool falsely reports non-encrypted PDF as password-protected

## 증상
The `Read` tool falsely reports PDFs as "password-protected" when they are not encrypted. This has been observed repeatedly across multiple conversations.

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
Reading the PDF via PyMuPDF in the Bash tool works reliably as a fallback.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38530
