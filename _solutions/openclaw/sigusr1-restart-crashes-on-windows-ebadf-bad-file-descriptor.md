---
layout: solution
title: "SIGUSR1 restart crashes on Windows: EBADF bad file descriptor"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/19819
---

# SIGUSR1 restart crashes on Windows: EBADF bad file descriptor

## 증상
SIGUSR1-triggered gateway restarts consistently crash on Windows 10 with an \EBADF: bad file descriptor, write\ error. The spawned child process inherits file descriptors from the parent that are already closed/invalid by the time it tries to write to stdout/console.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Manual restart via \openclaw gateway stop && openclaw gateway start\ or a PowerShell restart script.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/19819
