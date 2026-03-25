---
layout: solution
title: "Telegram `replyToMode: all` does not reply to the triggering message in group/forum topics"
category: hallucination
source: https://github.com/openclaw/openclaw/issues/50326
---

# Telegram `replyToMode: all` does not reply to the triggering message in group/forum topics

## 증상
Behavior bug (incorrect output/state without crash)

## 원인
보고된 버그/문제. 카테고리: hallucination.

## 해결법
I can work around it locally by patching the Telegram delivery code, but that change is overwritten on upgrade, so it is not a real fix.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50326
