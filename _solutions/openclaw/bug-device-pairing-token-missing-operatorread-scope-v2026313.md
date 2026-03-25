---
layout: solution
title: "Bug: device pairing token missing operator.read scope (v2026.3.13)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/49192
---

# Bug: device pairing token missing operator.read scope (v2026.3.13)

## 증상
After device pairing, the token contains only `operator.admin` — `operator.read` is missing. Since `openclaw status` RPC requires `operator.read`, it fails with `missing scope: operator.read` even though the device has `operator.admin`.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
None found. Re-pairing reproduces the same issue.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/49192
