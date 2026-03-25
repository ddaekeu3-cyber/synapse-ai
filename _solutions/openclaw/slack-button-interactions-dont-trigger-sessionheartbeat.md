---
layout: solution
title: "Slack button interactions don't trigger session/heartbeat"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/33039
---

# Slack button interactions don't trigger session/heartbeat

## 증상
When a user clicks a Slack button (with `openclaw:` prefix in `action_id`), the interaction is captured and logged:

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Currently there's no workaround — the user must manually trigger the session (e.g., by sending a message) after clicking the button.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/33039
