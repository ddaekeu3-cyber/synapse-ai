---
layout: solution
title: "setup-podman.sh fails with 'cannot chdir: Permission denied' when run from a directory the openclaw user cannot access"
category: openclaw
---

# setup-podman.sh fails with 'cannot chdir: Permission denied' when run from a directory the openclaw user cannot access

## 증상
**Bug type:** Behavior bug (incorrect output/state without crash)

에러 메시지:
```
Loading image into openclaw's Podman store...
Using temporary image dir: /var/tmp
[... blob copying succeeds ...]
cannot chdir to /home/<user>/openclaw-attempt/openclaw/openclaw: Permission denied

## 원인
원본 이슈에서 확인 필요. GitHub Issue #39434 참조.

## 해결법
**

Wrap the call in a subshell that first `cd`s to a world-accessible directory:

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/39434
