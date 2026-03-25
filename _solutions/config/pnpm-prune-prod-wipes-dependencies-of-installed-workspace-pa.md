---
layout: solution
title: "pnpm prune --prod wipes dependencies of installed workspace packages in partial workspaces (pnpm 10)"
category: config
source: https://github.com/openclaw/openclaw/issues/49501
---

# pnpm prune --prod wipes dependencies of installed workspace packages in partial workspaces (pnpm 10)

## 증상
Regression (worked before, now fails)

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
is used.

### Additional information

This behavior was observed after upgrading to pnpm 10. Existing Docker builds that used 'pnpm prune --prod' to clean up devDeps after mounting the full source now result in empty node_modules for previously installed packages.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/49501
