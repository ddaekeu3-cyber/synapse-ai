---
layout: solution
title: "Feature request: --profile-id flag for `models auth login`"
category: auth
source: https://github.com/openclaw/openclaw/issues/40402
---

# Feature request: --profile-id flag for `models auth login`

## 증상
When using `openclaw models auth login --provider openai-codex` to log in with multiple OpenAI accounts, the OAuth flow always creates/overwrites `openai-codex:default` because OpenAI's OAuth doesn't return an email for profile ID naming.

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
`paste-token` already supports `--profile-id`, but OAuth-only providers (like OpenAI Codex) don't have manually extractable tokens. The current workaround is creating separate agents per account, which adds operational overhead.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/40402
