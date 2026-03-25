---
layout: solution
title: "Feishu: Add support for creating group chats (im:chat:create)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/27685
---

# Feishu: Add support for creating group chats (im:chat:create)

## 증상
The Feishu extension currently doesn't support creating group chats programmatically, even though the API permission `im:chat:create` is available and can be granted.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Users must manually create groups in Feishu and then provide the chat_id to the agent.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/27685
