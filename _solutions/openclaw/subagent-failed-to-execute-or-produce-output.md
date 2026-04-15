---
layout: solution
title: "subagent failed to execute or produce output"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/51062
description: "Regression (worked before, now"
---

# subagent failed to execute or produce output

## 증상
Regression (worked before, now fails)

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Currently, CAD tasks are successfully performed by the main agent directly executing `write-scad` and `render-scad` tools, as demonstrated by the creation of 20mm cube and Samsung S23 phone stand models.
```

You can copy and paste this text to submit your bug report. Please remember to fill in your current OpenClaw version at the top.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51062
