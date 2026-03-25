---
layout: solution
title: "Multi-channel session routing - replies go to wrong channel"
category: telegram
source: https://github.com/openclaw/openclaw/issues/45514
---

# Multi-channel session routing - replies go to wrong channel

## 증상
When receiving messages from multiple channels (Telegram, Feishu, Weibo), replies are sent to the wrong channel instead of the channel where the message originated.

## 원인
보고된 버그/문제. 카테고리: telegram.

## 해결법
None. User must disable other channels to receive replies correctly.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/45514
