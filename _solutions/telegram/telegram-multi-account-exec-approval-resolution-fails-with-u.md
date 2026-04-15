---
layout: solution
title: "Telegram multi-account exec approval resolution fails with 'unknown or expired approval id' on non-default accounts"
category: telegram
source: https://github.com/openclaw/openclaw/issues/53240
description: "Exec approval prompts are delivered correctly to non-default Telegram bot accounts, but resolving approvals (clicking approve / running ) from those"
---

# Telegram multi-account exec approval resolution fails with 'unknown or expired approval id' on non-default accounts

## 증상
Exec approval **prompts are delivered correctly** to non-default Telegram bot accounts, but **resolving approvals** (clicking approve / running `/approve`) from those accounts fails with:

## 원인
Telegram Bot API conflict, rate limit, or webhook/polling configuration error causing message delivery failure.

## 해결법
Approvals can only be resolved from the default bot. Non-default bots can deliver prompts, but the user must switch to the default bot's DM to actually approve. This is fragile and confusing for operators.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53240
