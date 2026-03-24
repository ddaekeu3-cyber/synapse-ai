---
layout: solution
title: "Windows Telegram execs fail in allowlist/command-rebuild path"
category: telegram
---

# Windows Telegram execs fail in allowlist/command-rebuild path

## 증상
On Windows, Telegram-triggered execs still fail in OpenClaw's allowlist/command-rebuild path before approval can help. The gateway restarts cleanly, but a simple PowerShell command is rebuilt into an invalid PowerShell invocation and/or denied with `exec denied: allowlist miss`.

에러 메시지:
`
   PowerShell rejected it with parser errors such as `

## 원인
원본 이슈에서 확인 필요. GitHub Issue #52952 참조.

## 해결법
the issue. I reverted those edits.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: telegram
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/52952
