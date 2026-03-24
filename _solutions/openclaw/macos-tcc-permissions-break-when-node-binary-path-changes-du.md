---
layout: solution
title: "macOS TCC permissions break when node binary path changes during update"
category: openclaw
---

# macOS TCC permissions break when node binary path changes during update

## 증상
When OpenClaw auto-updates via `update-openclaw` (or manual `npm install -g openclaw@latest`), if the underlying Node.js binary path changes (e.g. from `node@24` to `node@25`, or a Homebrew upgrade), **all macOS TCC (Transparency, Consent, and Control) permissions are silently lost**. This breaks:



## 원인
원본 이슈에서 확인 필요. GitHub Issue #22179 참조.

## 해결법
Manually copy TCC permissions from old node binary to new one:

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/22179
