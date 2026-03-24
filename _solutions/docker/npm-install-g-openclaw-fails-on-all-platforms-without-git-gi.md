---
layout: solution
title: "npm install -g openclaw fails on all platforms without Git — git-sourced transitive dependency libsignal-node"
category: docker
---

# npm install -g openclaw fails on all platforms without Git — git-sourced transitive dependency libsignal-node

## 증상
`npm install -g openclaw@latest` fails on any machine (Windows, macOS, Linux) that does not have Git installed or lacks GitHub SSH keys. The root cause is a transitive dependency `libsignal-node` sourced from a GitHub git repository instead of the npm registry.

에러 메시지:
```shell
npm error code 128
npm error An unknown git error occurred
npm error command git --no-replace-objects ls-remote ssh://git@github.com/whiskeysockets/libsignal-node.git
npm error git@github.com

## 원인
원본 이슈에서 확인 필요. GitHub Issue #43419 참조.

## 해결법
Since `@whiskeysockets/baileys` is only required for WhatsApp channel support, not core CLI functionality:

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: docker
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/43419
