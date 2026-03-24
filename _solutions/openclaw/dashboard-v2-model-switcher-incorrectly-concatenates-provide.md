---
layout: solution
title: "[Dashboard v2] Model switcher incorrectly concatenates provider prefix when switching between different providers"
category: openclaw
---

# [Dashboard v2] Model switcher incorrectly concatenates provider prefix when switching between different providers

## 증상
Regression (worked before, now fails)

에러 메시지:
` (Moonshot)

2. Open Dashboard v2 chat UI
3. Start with model A (e.g., 千问)
4. Use the model picker to switch to model B (e.g., Kimi)
5. Observe the error: `

## 원인
원본 이슈에서 확인 필요. GitHub Issue #52173 참조.

## 해결법
** instead of the **target model's configured provider**, resulting in an invalid model ID and "model not allowed" error.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/52173
