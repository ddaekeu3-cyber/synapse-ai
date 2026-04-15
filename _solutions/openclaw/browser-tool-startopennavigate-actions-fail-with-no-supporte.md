---
layout: solution
title: "Browser tool start/open/navigate actions fail with 'No supported browser found' even when Chrome is running and browser control service work"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/53004
description: "Regression (worked before, now"
---

# Browser tool start/open/navigate actions fail with "No supported browser found" even when Chrome is running and browser control service work

## 증상
Regression (worked before, now fails)

## 원인
GitHub Issue #53004에서 보고된 버그. 해결법이 이슈에 포함됨.

## 해결법
Start Chrome via the CLI once (`openclaw browser --browser-profile openclaw start`) after each gateway start, then use the browser control REST API directly on port 18791 for all automation (`/tabs/open`, `/snapshot`, `/act`, `/navigate`).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53004
