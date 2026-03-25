---
layout: solution
title: "Native installer on Windows: bash hooks resolve to WSL bash.exe instead of Git Bash, causing TUI hang with broken timeout"
category: performance
source: https://github.com/anthropics/claude-code/issues/37634
---

# Native installer on Windows: bash hooks resolve to WSL bash.exe instead of Git Bash, causing TUI hang with broken timeout

## 증상
- [x] I have searched existing issues and this has not been reported yet

## 원인
보고된 버그/문제. 카테고리: performance.

## 해결법
Use a Node.js wrapper that calls Git Bash via `execFileSync` with an absolute path, bypassing PATH resolution entirely:

```javascript
// bash-wrapper.js
const { execFileSync } = require("child_process");
const fs = require("fs");
const gitBash = "C:/Program Files/Git/bin/bash.exe";
if (!fs.existsSync(gitBash)) process.exit(1);
try {
  execFileSync(gitBash, process.argv.slice(2), {
    stdio: "inherit", timeout: 30000, windowsHide: true
  });
} catch (e) { process.exit(e.status || 0); }
```

Then in hooks: `node bash-wrapper.js myscript.sh`

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37634
