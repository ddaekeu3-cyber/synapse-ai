---
layout: solution
title: "Feature: Activity-aware run timeout (wall-clock timeout kills active multi-tool runs)"
category: openclaw
---

# Feature: Activity-aware run timeout (wall-clock timeout kills active multi-tool runs)

## 증상
The gateway run timeout (`agents.defaults.timeoutSeconds`, default 600s) is a flat wall-clock timer. It kills runs that exceed the limit regardless of whether they are actively making progress.



## 원인
원본 이슈에서 확인 필요. GitHub Issue #41588 참조.

## 해결법
Bump `agents.defaults.timeoutSeconds` to a higher value (e.g. 1200). This is a band-aid — long research sessions can still hit it.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/41588
