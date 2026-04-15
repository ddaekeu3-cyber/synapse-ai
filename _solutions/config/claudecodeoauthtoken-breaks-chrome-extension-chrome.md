---
layout: solution
title: "CLAUDE_CODE_OAUTH_TOKEN breaks Chrome extension (--chrome)"
category: config
source: https://github.com/anthropics/claude-code/issues/29924
description: "Setting the environment variable causes the Chrome extension ( / Claude in Chrome) to stop working. The extension reports \"browser extension is not"
---

# CLAUDE_CODE_OAUTH_TOKEN breaks Chrome extension (--chrome)

## 증상
Setting the `CLAUDE_CODE_OAUTH_TOKEN` environment variable causes the Chrome extension (`--chrome` / Claude in Chrome) to stop working. The extension reports "browser extension is not connected" even though it shows "Connected" in Chrome toolbar.

## 원인
Environment variable, configuration file, or initialization parameter missing, malformed, or incorrectly scoped.

## 해결법
Don't set `CLAUDE_CODE_OAUTH_TOKEN` when using `--chrome`. Use browser login only (`~/.claude/.credentials.json`).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/29924
