---
layout: solution
title: "Telegram bold/italic entities not rendered as markdown in agent prompt (regression)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/52859
---

# Telegram bold/italic entities not rendered as markdown in agent prompt (regression)

## 증상
**Version:** OpenClaw 2026.3.13 (61d171a)

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
None available currently — hooks don't intercept inbound messages before agent processing.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/52859
