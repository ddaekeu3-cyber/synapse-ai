---
layout: solution
title: "Subagent announce retry too short — fails silently on transient WhatsApp disconnects"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/46081
---

# Subagent announce retry too short — fails silently on transient WhatsApp disconnects

## 증상
Subagent announce completions fail silently when the WhatsApp (Baileys) listener experiences a brief disconnection. The current retry strategy (4 attempts over ~40 seconds) is insufficient for transient outages that resolve within 1-2 minutes.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
1. OpenClaw 최신 버전으로 업데이트: `npm update -g openclaw`
2. Gateway 재시작: `openclaw gateway restart`
3. 설정 파일 확인: `~/.openclaw/config.yaml`
4. 로그 확인: `openclaw logs --tail 50`
5. 원본 GitHub Issue에서 패치 확인

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/46081
