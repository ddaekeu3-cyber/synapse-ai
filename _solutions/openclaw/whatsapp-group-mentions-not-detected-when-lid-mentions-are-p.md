---
layout: solution
title: "WhatsApp group @mentions not detected when LID mentions are present (wasMentioned=false even though normalizedMentionedJids matches selfE164)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/49317
description: "Regression (worked before, now"
---

# WhatsApp group @mentions not detected when LID mentions are present (wasMentioned=false even though normalizedMentionedJids matches selfE164)

## 증상
Regression (worked before, now fails)

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
If group activation is set to “always” / requireMention: false, the bot can still respond without mention detection.
But mention detection itself remains incorrect (still logs wasMentioned=false).
SUGGESTED FIX

In the hasMentions && isSelfChat path, also check:
targets.normalizedMentions.includes(targets.selfE164) and/or targets.selfJid
Or change the definition of “self chat” so it does not trigger for group messages based on allowFrom.
Remove the empty else if (hasMentions && isSelfChat) {} fall-through.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/49317
