---
layout: solution
title: "Compaction late success leaves /status compactionCount stale after timeout"
category: openclaw
---

# Compaction late success leaves /status compactionCount stale after timeout

## 증상
A manual compaction on session `agent:christina:feishu:group:oc_03f1133e89a8d5ee60128a2c3ebca80a` reported:

에러 메시지:
```text
Compaction failed: Compaction timed out
```

## 원인
원본 이슈에서 확인 필요. GitHub Issue #45492 참조.

## 해결법
Direction

### Preferred fix: post-compaction reconciliation write

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/45492
