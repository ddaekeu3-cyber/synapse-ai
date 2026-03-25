---
layout: solution
title: "memory.qmd.update.embedInterval never triggers qmd embed"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/37326
---

# memory.qmd.update.embedInterval never triggers qmd embed

## 증상
`memory.qmd.update.embedInterval` is configured but never actually executes `qmd embed`. The interval setting is accepted by the config, the gateway logs "qmd memory startup initialization armed" on every restart, but no embed runs ever fire.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Run `qmd embed` manually or set up a system cron.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/37326
