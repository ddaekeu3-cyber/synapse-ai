---
layout: solution
title: "npm install fails on Windows: git@github.com permission denied for libsignal-node"
category: openclaw
---

# npm install fails on Windows: git@github.com permission denied for libsignal-node

## 증상
Regression (worked before, now fails)

에러 메시지:
```
git@github.com: Permission denied (publickey).
fatal: Could not read from remote repository.
```

### Expected behavior

Installation should succeed without requiring SSH configuration, OR
Provide

## 원인
원본 이슈에서 확인 필요. GitHub Issue #40684 참조.

## 해결법
with:
```
npm error code 128
npm error command git --no-replace-objects ls-remote ssh://[git@github.com](mailto:git@github.com)/whiskeysockets/libsignal-node.git
npm error [git@github.com](mailto:git@github.com): Permission denied (publickey).
```
No further progress is made; the process exits with code 128. This occurs even after configuring `git config url."https://github.com/".insteadOf="git@gi

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/40684
