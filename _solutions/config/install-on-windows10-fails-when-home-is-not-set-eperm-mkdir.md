---
layout: solution
title: "Install on Windows10 fails when HOME is not set.  EPERM mkdir '\' fails"
category: config
source: https://github.com/anthropics/claude-code/issues/31895
---

# Install on Windows10 fails when HOME is not set.  EPERM mkdir '\' fails

## 증상
- [x] I have searched [existing issues](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20state%3Aopen%20label%3Abug) and this hasn't been reported yet

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
was simple, just define HOME prior to running the installer.
```
set HOME=%USERPROFILE%
curl -fsSL https://claude.ai/install.cmd -o install.cmd && install.cmd
Setting up Claude Code...
√ Claude Code successfully installed!
  Version: 2.1.71
  Location: C:\Users\user1\.local\bin\claude.exe
  Next: Run claude --help to get started
‼ Setup notes:
  • Native installation exists but C:\Users\user1\.local\bin is not in your PATH. Add it by opening: System
  Properties → Environment Variables → Edit User PATH → New → Add the path above. Then restart your terminal.
Installation complete!
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/31895
