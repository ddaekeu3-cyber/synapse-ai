---
layout: solution
title: "Discord: empty components forces v2 path, silently drops media attachments"
category: general
source: https://github.com/openclaw/openclaw/issues/49703
---

# Discord: empty components forces v2 path, silently drops media attachments

## 증상
When using the `message` tool with `action=send` on Discord, the `components` parameter is **required by the tool schema** (must be an object). This forces every `message send` call to pass `components`, even when the intent is to send a plain message with a media attachment.

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
Use the `MEDIA:` tag in the reply text instead of the `message` tool's `media` parameter. This routes through the reply delivery pipeline which uses the regular message path.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/49703
