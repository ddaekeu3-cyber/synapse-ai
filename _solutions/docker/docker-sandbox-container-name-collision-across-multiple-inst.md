---
layout: solution
title: "Docker sandbox container name collision across multiple instances on same host"
category: docker
source: https://github.com/openclaw/openclaw/issues/51363
---

# Docker sandbox container name collision across multiple instances on same host

## 증상
- OpenClaw version: v2026.3.13 (61d171a)

## 원인
보고된 버그/문제. 카테고리: docker.

## 해결법
Disable sandbox on all co-hosted instances by removing `agents.defaults.sandbox` from config. This loses sandbox security isolation.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51363
