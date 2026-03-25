---
layout: solution
title: "Discord (and other extensions) broken in Docker after send-deps extraction in #46301"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/46443
---

# Discord (and other extensions) broken in Docker after send-deps extraction in #46301

## 증상
PR #46301 ("Fix configure startup stalls from outbound send-deps imports") extracted `resolveOutboundSendDep` from `src/infra/outbound/deliver.ts` into a new file `src/infra/outbound/send-deps.ts`, and updated extension imports accordingly. This breaks all affected extensions (Discord, Telegram, WhatsApp, Slack, Signal, iMessage, Matrix, MSTeams) when running in Docker.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Check out a commit prior to #46301 (e.g. `d4ab73174` from 2026-03-07) and rebuild the Docker image.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/46443
