---
layout: solution
title: "Feature Request: Discord System Access Control (Owner/Admin Gate)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/28137
---

# Feature Request: Discord System Access Control (Owner/Admin Gate)

## 증상
When OpenClaw is configured for Discord in a shared server, there's currently no native way to restrict system-level operations (file access, command execution) to specific Discord users. This creates a security risk:

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Users must manually configure security rules in workspace files:

```markdown

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/28137
