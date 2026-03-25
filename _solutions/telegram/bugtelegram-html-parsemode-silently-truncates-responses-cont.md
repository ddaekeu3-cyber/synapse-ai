---
layout: solution
title: "bug(telegram): HTML parse_mode silently truncates responses containing angle-bracket tags (e.g. <think>)"
category: telegram
source: https://github.com/openclaw/openclaw/issues/49104
---

# bug(telegram): HTML parse_mode silently truncates responses containing angle-bracket tags (e.g. <think>)

## 증상
When the agent generates a response containing angle-bracket tags (e.g. `<think>`, `<thinking>`, `<tool>`) and the Telegram delivery pipeline sends with `parse_mode=HTML`, Telegram's HTML parser interprets those strings as HTML elements. If the tag is not in Telegram's allowed HTML subset, the tag and everything after it is silently dropped.

## 원인
보고된 버그/문제. 카테고리: telegram.

## 해결법
None automatic. Users can avoid triggering the bug by not referencing angle-bracket tokens in conversation, but this is not a reliable safeguard — the agent may produce them in any response.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/49104
