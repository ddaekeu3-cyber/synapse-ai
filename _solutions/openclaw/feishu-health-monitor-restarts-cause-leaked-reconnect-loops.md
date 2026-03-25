---
layout: solution
title: "Feishu health-monitor restarts cause leaked reconnect loops in @larksuiteoapi/node-sdk WSClient"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/40451
---

# Feishu health-monitor restarts cause leaked reconnect loops in @larksuiteoapi/node-sdk WSClient

## 증상
The Feishu channel health-monitor periodically detects the connection as `stuck` and triggers a restart (~every 35 minutes). Due to a bug in `@larksuiteoapi/node-sdk`'s `WSClient.reConnect()` ([reported upstream: larksuite/node-sdk#177](https://github.com/larksuite/node-sdk/issues/177)), each restart leaks the previous reconnect loop's `setTimeout` handles, causing unbounded parallel reconnect att

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
1. OpenClaw 최신 버전으로 업데이트: `npm update -g openclaw`
2. Gateway 재시작: `openclaw gateway restart`
3. 설정 파일 확인: `~/.openclaw/config.yaml`
4. 로그 확인: `openclaw logs --tail 50`
5. 원본 GitHub Issue에서 패치 확인

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/40451
