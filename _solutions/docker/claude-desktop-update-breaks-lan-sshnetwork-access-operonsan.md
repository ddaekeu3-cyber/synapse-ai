---
layout: solution
title: "Claude Desktop update breaks LAN SSH/network access (OPERON_SANDBOXED_NETWORK=1)"
category: docker
source: https://github.com/anthropics/claude-code/issues/37994
---

# Claude Desktop update breaks LAN SSH/network access (OPERON_SANDBOXED_NETWORK=1)

## 증상
After a Claude Desktop update (March 23, 2026), all local network access from Claude Code running inside Claude Desktop is blocked. SSH, SCP, ping, curl to LAN hosts all fail with "No route to host". This was working fine before the update.

## 원인
보고된 버그/문제. 카테고리: docker.

## 해결법
Run Claude Code from the CLI (`claude` command in terminal) instead of Claude Desktop — the CLI does not have the network sandbox.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37994
