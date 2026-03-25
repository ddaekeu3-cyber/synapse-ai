---
layout: solution
title: "[Plugin: Telegram] Inbound notifications never delivered to session — outbound works fine"
category: telegram
source: https://github.com/anthropics/claude-code/issues/36771
---

# [Plugin: Telegram] Inbound notifications never delivered to session — outbound works fine

## 증상
- [x] I have searched [existing issues](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20state%3Aopen%20label%3Abug) and this hasn't been reported yet

## 원인
보고된 버그/문제. 카테고리: telegram.

## 해결법
Using direct `curl` to Telegram Bot API for outbound. No workaround for inbound — user must paste messages manually in the terminal.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/36771
