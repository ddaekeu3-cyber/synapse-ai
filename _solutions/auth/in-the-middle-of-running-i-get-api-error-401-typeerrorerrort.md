---
layout: solution
title: "In the middle of running I get `⎿ API Error: 401 {'type':'error','error':{'type':'authentication_error','message':'OAuth authentication is currently not supported.'}}`"
category: auth
source: https://github.com/anthropics/claude-code/issues/5893
description: "In the middle of running I"
---

# In the middle of running I get `⎿ API Error: 401 {"type":"error","error":{"type":"authentication_error","message":"OAuth authentication is currently not supported."}}`

## 증상
In the middle of running I get:

## 원인
Authentication credential mismatch, expiry, or permission scope gap between the requesting agent and the target API.

## 해결법
below already:

> `/logout` and log back in. 

 _Originally posted by @adagradschool in [#3624](https://github.com/anthropics/claude-code/issues/3624#issuecomment-3079828577)_

I logged out and login again, then tried to resume the conversation, **but I still encountered the same error**.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/5893
