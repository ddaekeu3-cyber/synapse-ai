---
layout: solution
title: "attachAs.mountPath not honored for runtime: 'subagent' attachments"
category: gog
---

# attachAs.mountPath not honored for runtime: "subagent" attachments

## 증상
Behavior bug (incorrect output/state without crash)

에러 메시지:
```shell
Observed evidence:

- Before enabling attachments, `sessions_spawn` with attachments failed with:
`attachments are disabled for sessions_spawn (enable tools.sessions_spawn.attachments.enabled

## 원인
원본 이슈에서 확인 필요. GitHub Issue #53249 참조.

## 해결법
is to omit `attachAs.mountPath` and rely on the runtime’s default internal attachment location.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: gog
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/53249
