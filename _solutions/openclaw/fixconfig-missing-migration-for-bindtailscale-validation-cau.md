---
layout: solution
title: "fix(config): missing migration for bind+tailscale validation causes crash loop"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/40910
---

# fix(config): missing migration for bind+tailscale validation causes crash loop

## 증상
Commit `53d10f868` (2026-03-02, `fix(gateway): land access/auth/config migration cluster`) added `validateGatewayTailscaleBind()` which rejects `gateway.bind="lan"` when `gateway.tailscale.mode="serve"`. However, no legacy migration was added to auto-fix existing configs with this combination.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
the `bind` + `tailscale.mode` conflict.
- The error message suggests `openclaw doctor --fix`, but the gateway is already dead so users can't easily see or act on it.
- Before this commit, `bind="lan"` + `tailscale.mode="serve"` worked without validation — this is a breaking change for existing installs.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/40910
