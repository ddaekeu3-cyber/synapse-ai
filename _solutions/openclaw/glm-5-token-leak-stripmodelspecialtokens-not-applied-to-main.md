---
layout: solution
title: "GLM-5 token leak: stripModelSpecialTokens not applied to main output path"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/44179
---

# GLM-5 token leak: stripModelSpecialTokens not applied to main output path

## 증상
OpenClaw 3.11 implemented a fix for GLM-5 token leakage (`stripModelSpecialTokens`), but the fix is not applied to the main chat output path, so users still see token leakage in their conversations.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
is not applied to the main chat output path, so users still see token leakage in their conversations.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/44179
