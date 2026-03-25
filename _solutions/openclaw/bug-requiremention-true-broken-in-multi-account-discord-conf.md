---
layout: solution
title: "Bug: requireMention: true broken in multi-account Discord config — all messages return no-mention"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/45300
---

# Bug: requireMention: true broken in multi-account Discord config — all messages return no-mention

## 증상
`requireMention: true` does not work in a multi-account Discord configuration. Every guild message is dropped at the preflight stage with `reason: "no-mention"`, even when the bot is explicitly @mentioned by a user in the allowlist.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Setting `requireMention: false` at the guild level restores functionality. The `groupPolicy: allowlist` + `users` array provides equivalent access control.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/45300
