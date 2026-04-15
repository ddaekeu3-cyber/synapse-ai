---
layout: solution
title: "Gateway silently fails when using legacy CLAWDBOT_* env variables"
category: config
source: https://github.com/openclaw/openclaw/issues/53482
description: "Gateway starts but does not function correctly. No error messages. All features appear broken. Environment variables set but not being"
---

# Gateway silently fails when using legacy CLAWDBOT_* env variables

## 증상
Gateway starts but does not function correctly. No error messages. All features appear broken. Environment variables set but not being used.

## 원인
Legacy CLAWDBOT_* environment variable names are no longer recognized. New versions use OPENCLAW_* prefix. No deprecation warning is shown.

## 해결법
### 레거시 환경변수 전환
1. 모든 `CLAWDBOT_*` 환경변수를 `OPENCLAW_*`로 변경:
   ```
   CLAWDBOT_TOKEN → OPENCLAW_TOKEN
   CLAWDBOT_CONFIG_DIR → OPENCLAW_CONFIG_DIR
   ```
2. `.env` 파일 전체 검색/치환
3. systemd service 파일도 업데이트
4. Docker compose의 environment 섹션도 확인

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53482
