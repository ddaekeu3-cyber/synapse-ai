---
layout: solution
title: "ERROR: _.reduce is not a function in jSq token counting function"
category: general
source: https://github.com/anthropics/claude-code/issues/38302
---

# ERROR: _.reduce is not a function in jSq token counting function

## 증상
The `jSq` function (token counting logic) throws `TypeError: _.reduce is not a function` when processing MCP tool output that is neither a string nor an array.

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
Set `MAX_MCP_OUTPUT_TOKENS=0` to disable token limiting, or avoid MCP tool patterns that return non-array/non-string content.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38302
