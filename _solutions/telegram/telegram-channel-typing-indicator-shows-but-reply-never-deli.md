---
layout: solution
title: "Telegram channel: typing indicator shows but reply never delivers with --channels flag"
category: telegram
source: https://github.com/anthropics/claude-code/issues/38171
---

# Telegram channel: typing indicator shows but reply never delivers with --channels flag

## 증상
**Version**: Claude Code 2.1.81

## 원인
보고된 버그/문제. 카테고리: telegram.

## 해결법
Calling reply tool from a normal interactive session works fine.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38171
