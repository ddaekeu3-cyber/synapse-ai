---
layout: solution
title: "Session history not visible when workspace is on a mapped network drive (Windows)"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/38186
description: "- Claude Code Extension:"
---

# Session history not visible when workspace is on a mapped network drive (Windows)

## 증상
- **Claude Code Extension**: v2.1.81

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Create a **directory junction** (Windows equivalent of symlink) from the UNC-based directory name to the drive-letter-based one:

```powershell
New-Item -ItemType Junction `
  -Path  "$env:USERPROFILE\.claude\projects\--192-168-x-x-share-path-to-project" `
  -Target "$env:USERPROFILE\.claude\projects\y--path-to-project"
```

After reloading VS Code, all historical sessions appear correctly.

**Note**: `mklink /J` may fail with _"local NTFS volumes required"_ — use PowerShell `New-Item -ItemType Junction` instead.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38186
