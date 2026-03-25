---
layout: solution
title: "Plugin cache creates infinite recursive directories when marketplace.json and plugin.json coexist in same repo"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/35842
---

# Plugin cache creates infinite recursive directories when marketplace.json and plugin.json coexist in same repo

## 증상
When a plugin repository contains both `.claude-plugin/marketplace.json` and `.claude-plugin/plugin.json` at the root level (self-hosted marketplace), `claude plugin add` creates an infinitely recursive directory structure in the plugin cache.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Move `marketplace.json` to a separate repository, following the pattern used by `claude-plugins-official`.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/35842
