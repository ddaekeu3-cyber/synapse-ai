---
layout: solution
title: "Claude Code repeatedly ignores explicit CLAUDE.md instructions and saved feedback memories across sessions"
category: auth
source: https://github.com/anthropics/claude-code/issues/37857
---

# Claude Code repeatedly ignores explicit CLAUDE.md instructions and saved feedback memories across sessions

## 증상
- [x] I have searched [existing issues](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20state%3Aopen%20label%3Amodel) for similar behavior reports

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
had a second bug (invalid CST value) that would have
  been caught by a single test
    - Reported a tarifa fix as "done" without actually deploying the frontend build to the server — the user's colleague
   confirmed "nothing changed"
    - After being corrected about not deploying, deployed but still did not follow the full protocol (no commit)
  5. The instructions are not ambiguous. They are written as explicit rules with bold formatting, tables of what to
  verify, and step-by-step protocols. Claude can recite them verbatim when asked but does not execute them.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37857
