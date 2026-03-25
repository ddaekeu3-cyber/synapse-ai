---
layout: solution
title: "Feishu streaming card output produces cumulative token duplication"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/38943
---

# Feishu streaming card output produces cumulative token duplication

## 증상
When `channels.feishu.streaming: true` with `renderMode: "card"`, Feishu bot replies exhibit **cumulative token duplication** — each new streaming chunk re-sends all previous content, resulting in a staircase-like repetition pattern in the final message.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Setting `streaming: false` at the channel level resolves the issue — agents wait for the full response before sending a single complete message. Per-bot override (`"streaming": false` on individual bot entries) also works.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/38943
