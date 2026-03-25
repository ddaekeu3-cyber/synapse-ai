---
layout: solution
title: "MS Teams: Inline images (Ctrl+V) in DMs not downloaded - Graph fallback fails"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/23453
---

# MS Teams: Inline images (Ctrl+V) in DMs not downloaded - Graph fallback fails

## 증상
Inline images pasted with Ctrl+V in Teams DMs are detected by the plugin but never downloaded. The log shows:

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Use the file attachment button (paperclip) instead of Ctrl+V to send images to the bot.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/23453
