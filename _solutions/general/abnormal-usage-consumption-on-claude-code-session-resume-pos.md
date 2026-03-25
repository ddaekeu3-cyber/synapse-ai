---
layout: solution
title: "Abnormal Usage Consumption on Claude Code Session Resume — Possible Bug"
category: general
source: https://github.com/anthropics/claude-code/issues/38029
---

# Abnormal Usage Consumption on Claude Code Session Resume — Possible Bug

## 증상
- [x] I have searched [existing issues](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20state%3Aopen%20label%3Abug) and this hasn't been reported yet

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
"Improved memory usage and startup time when resuming large sessions." This strongly suggests that the behavior I experienced was a known issue that has since been patched.

Request:
Could you please investigate whether this was indeed a bug in the session resume logic prior to the fix? If confirmed, I would appreciate a restoration of the usage credits consumed by this unintended behavior.

I have screenshots of the ccusage breakdown report available if needed.

Thank you for your time.

Best regards,
Yuki Kokemizawa
Seiyokoke-en (西予苔園)

![Image](https://github.com/user-attachments/assets/73c

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38029
