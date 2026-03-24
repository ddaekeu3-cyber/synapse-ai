---
layout: solution
title: "write tool: ~ path expansion resolves to wrong path causing EACCES"
category: openclaw
---

# write tool: ~ path expansion resolves to wrong path causing EACCES

## 증상
The `write` tool fails with `EACCES: permission denied, mkdir /home/vela/` when attempting to write to a path using `~` expansion.



## 원인
원본 이슈에서 확인 필요. GitHub Issue #50227 참조.

## 해결법
Use `exec` with `cat >` to write files instead.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/50227
