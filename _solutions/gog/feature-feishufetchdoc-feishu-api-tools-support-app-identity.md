---
layout: solution
title: "[Feature]: feishu_fetch_doc / Feishu API tools: support app-identity token for non-owner users"
category: gog
---

# [Feature]: feishu_fetch_doc / Feishu API tools: support app-identity token for non-owner users

## 증상
Allow non-owner Feishu users to use feishu_fetch_doc and other Feishu API tools via app-identity token fallback.

에러 메시지:
` (app identity) permission, the error persists: `

## 원인
원본 이슈에서 확인 필요. GitHub Issue #53606 참조.

## 해결법
When a non-owner user calls a Feishu tool:
1. Try user OAuth first (if they have an existing valid token)
2. If no user token available, fall back to app-identity token (tenant_access_token)
3. This allows team members to use the bot for document reading as long as the Feishu app has the corresponding app-identity permissions granted (e.g. `docs:doc:readonly`)

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: gog
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/53606
