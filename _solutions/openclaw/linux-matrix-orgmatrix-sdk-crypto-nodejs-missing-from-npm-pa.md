---
layout: solution
title: "Linux: @matrix-org/matrix-sdk-crypto-nodejs missing from npm package (only darwin-arm64 binary bundled)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/53785
---

# Linux: @matrix-org/matrix-sdk-crypto-nodejs missing from npm package (only darwin-arm64 binary bundled)

## 증상
Regression (worked before, now fails)

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
cd /usr/lib/node_modules/openclaw && npm install @matrix-org/matrix-sdk-crypto-nodejs

    Environment: Linux x64, OpenClaw 2026.3.23-2, Node 22.22.1

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53785
