---
layout: solution
title: "Cowork crashes immediately after splash screen on Windows 11 25H2 (Build 26200) — white screen then exit"
category: config
source: https://github.com/anthropics/claude-code/issues/34579
---

# Cowork crashes immediately after splash screen on Windows 11 25H2 (Build 26200) — white screen then exit

## 증상
- [x] I have searched [existing issues](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20state%3Aopen%20label%3Abug) and this hasn't been reported yet

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
missing locale file | Same crash
Change locale from ko-KR to en-US in config | Same crash
Windows App Settings → Repair | "Cannot repair this app"
McAfee antivirus | Expired/inactive — not the cause

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/34579
