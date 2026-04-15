---
layout: solution
title: "message tool: filePath media upload fails with 'Channel is unavailable: telegram' in 2026.3.23-2"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/53879
description: "Version: OpenClaw 2026.3.23-2"
---

# message tool: filePath media upload fails with 'Channel is unavailable: telegram' in 2026.3.23-2

## 증상
**Version:** OpenClaw 2026.3.23-2 (7ffe7e4)

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Direct Telegram Bot API calls via `curl` work perfectly:
```bash
curl -X POST 'https://api.telegram.org/bot<token>/sendDocument' \
  -F chat_id=<id> \
  -F document=@/path/to/file.pdf
```

This confirms the Telegram API and bot token are fine — the bug is in OpenClaw's message tool pipeline.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53879
