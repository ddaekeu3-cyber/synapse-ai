---
layout: solution
title: "Streaming freezes behind CGNAT (Starlink) due to missing SSE keepalives during thinking"
category: performance
source: https://github.com/anthropics/claude-code/issues/37534
---

# Streaming freezes behind CGNAT (Starlink) due to missing SSE keepalives during thinking

## 증상
Claude Code sessions freeze/hang during extended thinking phases when the user is behind CGNAT (confirmed on Starlink). The connection silently drops after ~60 seconds of inactivity because CGNAT routers kill TCP connections that appear idle.

## 원인
보고된 버그/문제. 카테고리: performance.

## 해결법
Using Cloudflare WARP (free VPN) resolves the issue by tunneling traffic past the CGNAT layer so it can't detect/kill the idle connection.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37534
