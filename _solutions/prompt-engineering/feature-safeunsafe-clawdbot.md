---
layout: solution
title: "[Feature]: safe/unsafe ClawdBot"
category: prompt-engineering
source: https://github.com/openclaw/openclaw/issues/6731
---

# [Feature]: safe/unsafe ClawdBot

## 증상
perhaps inherit this feature or abstraction from Rust, completely rewrite this project in Rust. Safe mode can be running ClawdBot locally in a sandbox environment with limited access and it protects user from undefined behaviour, memory leaks and root access from who knows outside, and unsafe it will be given root permission as it has now to do whatever you want at your own risk, prompt injections

## 원인
보고된 버그/문제. 카테고리: prompt-engineering.

## 해결법
. Otherwise what is the point of this project..?  Leak all user data..?

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/6731
