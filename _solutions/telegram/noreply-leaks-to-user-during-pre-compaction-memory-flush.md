---
layout: solution
title: "NO_REPLY leaks to user during pre-compaction memory flush"
category: telegram
source: https://github.com/openclaw/openclaw/issues/50437
description: "Isolated cron jobs with delivery mode \"announce\" deliver NO_REPLY as a visible message to the user instead of suppressing"
---

# NO_REPLY leaks to user during pre-compaction memory flush

## 증상
Isolated cron jobs with delivery mode "announce" deliver NO_REPLY as a visible message to the user instead of suppressing delivery.

## 원인
Telegram Bot API conflict, rate limit, or webhook/polling configuration error causing message delivery failure.

## 해결법
Set delivery mode to "none" for cron jobs that should be silent when there is nothing to report. Use the message tool within the cron session to actively notify the user when important content is found. This avoids the NO_REPLY suppression issue entirely because the agent never relies on auto-delivery.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50437
