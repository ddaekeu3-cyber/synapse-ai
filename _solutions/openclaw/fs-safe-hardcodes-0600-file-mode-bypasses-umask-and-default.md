---
layout: solution
title: "fs-safe hardcodes 0600 file mode, bypasses umask and default ACLs"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/32404
description: "The module hardcodes (octal , owner-only read/write) for all file writes. This bypasses both the system umask and any default POSIX ACLs set on parent"
---

# fs-safe hardcodes 0600 file mode, bypasses umask and default ACLs

## 증상
The `fs-safe` module hardcodes `fs.open(path, flags, 384)` (octal `0600`, owner-only read/write) for all file writes. This bypasses both the system **umask** and any **default POSIX ACLs** set on parent directories.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Periodic `find /path -type f -user agent_smith -perm 0600 -exec chmod g+rw {} +` via infrastructure audit script.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/32404
