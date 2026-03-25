---
layout: solution
title: "[Feature]: Webhook hook sessions should reuse existing session when sessionKey is consistent (multi-turn support)"
category: general
source: https://github.com/openclaw/openclaw/issues/11665
---

# [Feature]: Webhook hook sessions should reuse existing session when sessionKey is consistent (multi-turn support)

## 증상
The docs for `/hooks/agent` state:

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
Load conversation history from an external database into the prompt on every webhook invocation. Works but wastes tokens and adds latency.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/11665
