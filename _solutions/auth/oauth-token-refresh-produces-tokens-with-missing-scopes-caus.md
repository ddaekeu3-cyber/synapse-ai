---
layout: solution
title: "OAuth token refresh produces tokens with missing scopes, causing repeated 403 and re-login prompts"
category: auth
source: https://github.com/anthropics/claude-code/issues/34785
---

# OAuth token refresh produces tokens with missing scopes, causing repeated 403 and re-login prompts

## 증상
After a token refresh cycle, Claude Code sometimes ends up with an OAuth access token that is missing required scopes (specifically `user:profile`). The token passes the local expiration check ("OAuth token check complete") but every subsequent API call fails with HTTP 403:

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
Manual `/login` each time the token expires. On Windows this is a single button click but still disruptive.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/34785
