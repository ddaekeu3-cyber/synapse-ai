---
layout: solution
title: "Bug: Plugin tools registered via api.registerTool() not available in agent runtime"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/50328
---

# Bug: Plugin tools registered via api.registerTool() not available in agent runtime

## 증상
Plugins that register tools via `api.registerTool()` factory function do not have their tools available in the agent runtime tool list.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Use hooks instead of tools for now (e.g., `command:new` hook for automatic memory capture/recall).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50328
