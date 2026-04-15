---
layout: solution
title: "openclaw_supervisor.ps1: unquoted paths break on Windows usernames with spaces"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/47484
description: "The auto-generated passes file paths to without quoting them. On any Windows machine where contains a space (e.g. ), the supervisor cannot restart the"
---

# openclaw_supervisor.ps1: unquoted paths break on Windows usernames with spaces

## 증상
The auto-generated `openclaw_supervisor.ps1` passes `.cmd` file paths to `cmd.exe /c` without quoting them. On any Windows machine where `%USERPROFILE%` contains a space (e.g. `C:\Users\John Smith`), the supervisor cannot restart the gateway or node host after a crash.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Wrap the path in escaped double quotes:

```powershell
-ArgumentList @("/c", "`"$gatewayCmd`"")
-ArgumentList @("/c", "`"$nodeCmd`"")
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/47484
