---
layout: solution
title: "Contact name resolution fans out to unconfigured channel providers (whatsapp absent from config, 'message <name>' in prompt triggers Unknown target errors)"
category: hallucination
source: https://github.com/openclaw/openclaw/issues/42080
---

# Contact name resolution fans out to unconfigured channel providers (whatsapp absent from config, "message <name>" in prompt triggers Unknown target errors)

## 증상
Behavior bug (incorrect output/state without crash)

## 원인
보고된 버그/문제. 카테고리: hallucination.

## 해결법
in #25625 guards one code path but not this one.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/42080
