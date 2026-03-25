---
layout: solution
title: "Native Slack token rotation support (oauth.v2.access refresh flow)"
category: auth
source: https://github.com/openclaw/openclaw/issues/42747
---

# Native Slack token rotation support (oauth.v2.access refresh flow)

## 증상
Add native support for Slack's token rotation feature, enabling OpenClaw to automatically refresh expiring bot tokens using the oauth.v2.access refresh_token grant without manual intervention or external tooling.

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
requires indefinite maintenance.

**Consequence:** Security teams at regulated organizations (e.g., government, healthcare, financial services) are specifically targeted by Slack's recommendation to enable token rotation. Lack of native support creates a gap between OpenClaw's security posture and industry expectations, adds undifferentiated engineering overhead, and introduces operational risk if the external daemon fails silently.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/42747
