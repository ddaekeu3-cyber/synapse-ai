---
layout: solution
title: "Telegram channel can be wedged by bad persisted session JSON; status should detect and offer recovery"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/43549
---

# Telegram channel can be wedged by bad persisted session JSON; status should detect and offer recovery

## 증상
Telegram stopped working because the persisted Telegram session JSON had become effectively unusable ("full" / oversized / likely corrupted enough to wedge normal operation). Running `openclaw status telegram` exposed the clue, but the troubleshooting flow did not guide toward inspecting or remediating the session state file.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Delete the problematic Telegram session JSON file, then restart the gateway.

(Obviously a built-in backup-and-reset flow would be much safer than asking users to manually remove internal state files.)

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/43549
