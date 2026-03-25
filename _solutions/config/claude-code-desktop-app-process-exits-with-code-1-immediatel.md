---
layout: solution
title: "Claude Code desktop app: process exits with code 1 immediately on every message send"
category: config
source: https://github.com/anthropics/claude-code/issues/37887
---

# Claude Code desktop app: process exits with code 1 immediately on every message send

## 증상
Claude Code in the desktop app (Claude.app) fails to respond to any messages. Every time a message is sent, the bundled Claude Code process exits with code 1 immediately (< 1 second), with no response.

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
None currently. The CLI works fine but the desktop Code mode is completely broken.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37887
