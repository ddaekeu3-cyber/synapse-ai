---
layout: solution
title: "fix(gateway): full process restart fails to respawn when forced drain timeout fires with active embedded runs"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/48063
---

# fix(gateway): full process restart fails to respawn when forced drain timeout fires with active embedded runs

## 증상
When a full gateway restart is triggered (e.g. by a plugin config change) while embedded sub-agent runs are still active, the gateway process exits but the replacement process never spawns. The result is a silent outage — the old process dies, no new process starts, and the macOS LaunchAgent does not recover the service.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Run `openclaw gateway start` manually. Service recovers immediately with no data loss.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/48063
