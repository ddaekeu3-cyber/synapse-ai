---
layout: solution
title: "Gemini CLI OAuth broken on Windows (nvm)"
category: auth
source: https://github.com/openclaw/openclaw/issues/41800
---

# Gemini CLI OAuth broken on Windows (nvm)

## 증상
Gemini CLI OAuth is broken on Windows when `gemini-cli` is installed via nvm. Two independent bugs prevent setup from completing.

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
PR #40729 addresses both issues.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/41800
