---
layout: solution
title: "Native Windows executable crashes with segmentation fault (npm version works)"
category: general
source: https://github.com/anthropics/claude-code/issues/20869
---

# Native Windows executable crashes with segmentation fault (npm version works)

## 증상
- [x] I have searched [existing issues](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20state%3Aopen%20label%3Abug) and this hasn't been reported yet

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
Installing via npm (npm install -g @anthropic-ai/claude-code) works without issues.

Notes:

VSCode extension works fine (uses bundled runtime)
Issue started after switching from npm to native executable

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/20869
