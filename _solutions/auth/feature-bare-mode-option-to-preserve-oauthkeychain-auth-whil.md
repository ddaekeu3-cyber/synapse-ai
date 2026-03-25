---
layout: solution
title: "[FEATURE] --bare mode: option to preserve OAuth/keychain auth while skipping context"
category: auth
source: https://github.com/anthropics/claude-code/issues/38022
---

# [FEATURE] --bare mode: option to preserve OAuth/keychain auth while skipping context

## 증상
`--bare` mode strips **all** auto-discovery, including OAuth and keychain authentication. This makes it unusable for tools that need clean, context-free API calls but rely on the CLI's built-in auth (e.g. team/company subscriptions where there is no explicit API key).

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
We use `claude -p` without `--bare` and document that scores from the `claude-cli` provider may be less consistent than API-based providers due to injected local context.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38022
