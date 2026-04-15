---
layout: solution
title: "VS Code extension ignores proxy settings from ~/.claude/settings.json and environment variables"
category: config
source: https://github.com/anthropics/claude-code/issues/15684
description: "The VS Code extension does not respect proxy settings, while the CLI works correctly with the same"
---

# VS Code extension ignores proxy settings from ~/.claude/settings.json and environment variables

## 증상
The VS Code extension does not respect proxy settings, while the CLI works correctly with the same configuration.

## 원인
Environment variable, configuration file, or initialization parameter missing, malformed, or incorrectly scoped.

## 해결법
Currently the only workaround is to use `proxychains4` to force the proxy:
```bash
proxychains4 code
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/15684
