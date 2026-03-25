---
layout: solution
title: "Feature: Multi-agent group transcript for cross-agent visibility (Telegram, Signal, WhatsApp, and more)"
category: telegram
source: https://github.com/openclaw/openclaw/issues/54259
---

# Feature: Multi-agent group transcript for cross-agent visibility (Telegram, Signal, WhatsApp, and more)

## 증상
When multiple AI agents are bound to the same Telegram group chat, they cannot see each other's responses due to the Telegram Bot API limitation (bots cannot read other bot messages).

## 원인
보고된 버그/문제. 카테고리: telegram.

## 해결법
Users must manually maintain transcript files and configure agents to read/write them — which requires discipline and fails when agents forget.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/54259
