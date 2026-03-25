---
layout: solution
title: "[Windows] EEXIST error on mkdir ~/.claude when folder has ReadOnly attribute"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/37306
---

# [Windows] EEXIST error on mkdir ~/.claude when folder has ReadOnly attribute

## 증상
Claude Code intermittently fails with:

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
```cmd
attrib -R "C:\Users\<user>\.claude"
del "C:\Users\<user>\.claude\desktop.ini"
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37306
