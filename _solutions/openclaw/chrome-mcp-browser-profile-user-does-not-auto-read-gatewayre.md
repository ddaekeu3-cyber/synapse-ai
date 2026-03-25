---
layout: solution
title: "Chrome MCP (`--browser-profile user`) does not auto-read `gateway.remote.token`; `open` fails with gateway closed (1000)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/51661
---

# Chrome MCP (`--browser-profile user`) does not auto-read `gateway.remote.token`; `open` fails with gateway closed (1000)

## 증상
Regression / browser-gateway auth + session stability issue

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
A temporary workaround is to manually pass `--token` every time:

```bash
openclaw browser --browser-profile user --token "<token>" tabs
```

or define a shell alias.

However, this still does not fix the `open` / session stability problem.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51661
