---
layout: solution
title: "Sandbox env sanitizer blocks skill primaryEnv vars (e.g. NOTION_API_KEY)"
category: docker
source: https://github.com/openclaw/openclaw/issues/25951
description: "Built-in skills that declare with names matching the sandbox env sanitizer's blocklist cannot work in sandboxed"
---

# Sandbox env sanitizer blocks skill primaryEnv vars (e.g. NOTION_API_KEY)

## 증상
Built-in skills that declare `primaryEnv` with names matching the sandbox env sanitizer's blocklist cannot work in sandboxed agents.

## 원인
Container permission, networking, or environment variable misconfiguration inside the sandbox.

## 해결법
Rename the env var to bypass the pattern (e.g. `NOTION_KEY` instead of `NOTION_API_KEY` in `sandbox.docker.env`) and update the skill SKILL.md to reference the renamed var.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/25951
