---
layout: solution
title: "Claude Desktop 1.1.4173 crashes on startup on Windows (Squirrel install, sv-SE locale)"
category: config
source: https://github.com/anthropics/claude-code/issues/28470
description: "- [x] I have searched existing issues and this hasn't been reported"
---

# Claude Desktop 1.1.4173 crashes on startup on Windows (Squirrel install, sv-SE locale)

## 증상
- [x] I have searched [existing issues](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20state%3Aopen%20label%3Abug) and this hasn't been reported yet

## 원인
Environment variable, configuration file, or initialization parameter missing, malformed, or incorrectly scoped.

## 해결법
Add an `id` field to all `formatMessage()` calls in `WOt()` in the main process bundle, e.g.:
  `Ue.formatMessage({ id: "cowork.msix_required", defaultMessage: "Cowork requires Claude Desktop be installed with our
  modern installer" })`

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/28470
