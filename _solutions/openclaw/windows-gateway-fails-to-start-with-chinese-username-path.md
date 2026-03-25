---
layout: solution
title: "[Windows] Gateway fails to start with Chinese username path"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/43943
---

# [Windows] Gateway fails to start with Chinese username path

## 증상
Crash (process/app exits or hangs)

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Manually running the command works:
& "C:\Program Files\nodejs\node.exe" "C:\Users\幻14\AppData\Roaming\npm\node_modules\openclaw\dist\index.js" gateway run

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/43943
