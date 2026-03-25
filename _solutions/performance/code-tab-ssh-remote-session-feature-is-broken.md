---
layout: solution
title: "Code tab SSH remote session feature is broken."
category: performance
source: https://github.com/anthropics/claude-code/issues/37154
---

# Code tab SSH remote session feature is broken.

## 증상
The Code tab SSH remote session feature is broken. After connecting to a remote Linux VPS via SSH, sending any prompt causes `ccd-cli` to crash immediately with exit code 1. The session either hangs silently or disconnects.

## 원인
보고된 버그/문제. 카테고리: performance.

## 해결법
does not appear to be included in the current Desktop app release.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37154
