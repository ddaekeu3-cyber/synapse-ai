---
layout: solution
title: "google-gemini-cli OAuth became consistently slow per turn after upgrading to OpenClaw 2026.3.23-2"
category: gog
---

# google-gemini-cli OAuth became consistently slow per turn after upgrading to OpenClaw 2026.3.23-2

## 증상
Regression (worked before, now fails)

에러 메시지:
`

1. Upgrade OpenClaw to 2026.3.23-2.
2. Configure and use google-gemini-cli with OAuth.
3. Start a fresh/new session with minimal or no prior context.
4. Send a simple prompt such as:

• test speed


## 원인
원본 이슈에서 확인 필요. GitHub Issue #53578 참조.

## 해결법
ed per-turn auth / credential retrieval delay on the google-gemini-cli OAuth path.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: gog
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/53578
