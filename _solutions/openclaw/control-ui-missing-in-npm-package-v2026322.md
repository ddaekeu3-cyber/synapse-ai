---
layout: solution
title: "Control UI missing in npm package v2026.3.22"
category: openclaw
---

# Control UI missing in npm package v2026.3.22

## 증상
Regression (worked before, now fails)



## 원인
원본 이슈에서 확인 필요. GitHub Issue #52817 참조.

## 해결법
Manually clone repo, run `pnpm ui:build`, copy dist/control-ui/ to npm package directory OpenClaw via browser (Control UI)
Severity: High — Control UI completely inaccessible, only TUI works
Frequency: 100% (every fresh install of v2026.3.22)
Consequence: Users cannot use the web dashboard; workaround is to use TUI (terminal) instead

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/52817
