---
layout: solution
title: "Memory SQLite should use WAL journal mode by default"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/36035
description: "Memory index SQLite database () is created with (SQLite default). This causes frequent database corruption when the gateway receives SIGTERM during write"
---

# Memory SQLite should use WAL journal mode by default

## 증상
Memory index SQLite database (`~/.openclaw/memory/main.sqlite`) is created with `journal_mode=delete` (SQLite default). This causes frequent database corruption when the gateway receives SIGTERM during write operations (restart, update, config changes).

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
```bash
sqlite3 ~/.openclaw/memory/main.sqlite "PRAGMA journal_mode=WAL;"
```
But this resets if the DB is recreated (e.g., after `openclaw memory index` on a corrupted DB).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/36035
