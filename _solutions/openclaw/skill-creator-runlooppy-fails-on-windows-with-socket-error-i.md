---
layout: solution
title: "skill-creator: run_loop.py fails on Windows with socket error in claude -p subprocess"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/33467
---

# skill-creator: run_loop.py fails on Windows with socket error in claude -p subprocess

## 증상
The `skill-creator` plugin's `run_loop.py` / `run_eval.py` scripts fail on Windows when spawning `claude -p` subprocesses. The eval phase uses `claude -p` to test whether a skill triggers for a given query, but on Windows the subprocess crashes with:

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Run the optimization on a macOS or Linux machine instead. The encoding issue can be mitigated with `PYTHONUTF8=1`, but the socket error has no known workaround.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/33467
