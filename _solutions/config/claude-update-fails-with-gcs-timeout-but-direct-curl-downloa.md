---
layout: solution
title: "claude update fails with GCS timeout, but direct curl download works"
category: config
source: https://github.com/anthropics/claude-code/issues/37801
---

# claude update fails with GCS timeout, but direct curl download works

## 증상
`claude update` fails with a 30-second timeout when trying to fetch the latest version from GCS, even though the same URL is reachable via direct `curl`.

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
Manual native binary update:

```bash

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37801
