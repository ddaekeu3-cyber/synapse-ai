---
layout: solution
title: "setup-token OAuth PKCE sends lowercase s256 instead of S256 on Termux"
category: config
source: https://github.com/anthropics/claude-code/issues/22398
---

# setup-token OAuth PKCE sends lowercase s256 instead of S256 on Termux

## 증상
Running `claude setup-token` on Termux (Android) fails with:

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
Currently authenticating on a separate Linux machine and copying credentials to Termux manually.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/22398
