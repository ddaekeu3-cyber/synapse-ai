---
layout: solution
title: "Matrix Plugin API Version Mismatch After Upgrade to v2026.3.22"
category: openclaw
---

# Matrix Plugin API Version Mismatch After Upgrade to v2026.3.22

## 증상
Regression (worked before, now fails)

에러 메시지:
```
Config warnings: 
  - plugins.entries.matrix: plugin not found: matrix (stale config entry ignored; remove it from plugins config)   

Doctor warnings:
 - Matrix migration warnings are present, bu

## 원인
원본 이슈에서 확인 필요. GitHub Issue #52899 참조.

## 해결법
`, Matrix-related errors occur. Attempting to reinstall or repair the Matrix plugin (`openclaw plugins install @openclaw/matrix`) results in the same API version mismatch error.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/52899
