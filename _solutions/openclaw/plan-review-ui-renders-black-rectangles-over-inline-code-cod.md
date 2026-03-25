---
layout: solution
title: "Plan review UI renders black rectangles over inline code, code blocks, and tables"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/38486
---

# Plan review UI renders black rectangles over inline code, code blocks, and tables

## 증상
The plan review panel in the VS Code extension renders black rectangles/bars over certain markdown elements, making the plan unreadable. Affected elements include:

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Rewrite the plan using only plain text (no backticks, no code blocks, no pipe tables) to avoid the rendering issue. Users can also read the plan file directly in the editor instead of the review panel.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38486
