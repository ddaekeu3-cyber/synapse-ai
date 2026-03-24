---
layout: solution
title: "ACP `mode: 'run'` sessions leave orphan processes and lose completion events"
category: openclaw
---

# ACP `mode: "run"` sessions leave orphan processes and lose completion events

## 증상
Behavior bug (incorrect output/state without crash)

에러 메시지:
` shows "No sessions" after close, but the parent session was never notified.

## Suggested Fix

### For orphan processes

When the ACP run session completes (either successfully or via timeout/error)

## 원인
원본 이슈에서 확인 필요. GitHub Issue #52708 참조.

## 해결법
Command()` spawns the child process without `detached: true`, so the child inherits the parent's process group. However, when the intermediate acpx/queue-owner processes exit, they don't call `child.kill()` or `process.kill(-pgid)` before terminating. The OS reparents the children to init/systemd.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/52708
