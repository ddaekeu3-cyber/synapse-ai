---
layout: solution
title: "compile some errors"
category: openclaw
---

# compile some errors

## 증상
Regression (worked before, now fails)

에러 메시지:
```shell
npm run build
#15 21.08 ...p_32_862db5082615b32b3845591029415d74 npm-install: npm warn Unknown env config "npm-globalconfig". This will stop working in the next major version of npm.
#15 21.0

## 원인
원본 이슈에서 확인 필요. GitHub Issue #39781 참조.

## 해결법
#15 28.35 ...p_32_862db5082615b32b3845591029415d74 npm-install: To address all issues (including breaking changes), run:

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/39781
