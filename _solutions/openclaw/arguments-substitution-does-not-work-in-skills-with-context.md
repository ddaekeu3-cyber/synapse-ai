---
layout: solution
title: "$ARGUMENTS substitution does not work in skills with context: fork"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/34164
---

# $ARGUMENTS substitution does not work in skills with context: fork

## 증상
When a skill with `context: fork` in frontmatter is invoked by another skill via the `Skill()` tool, `$ARGUMENTS`, `$ARGUMENTS[0]`, and `$ARGUMENTS[1]` are **not substituted**. The forked context receives the raw template with literal placeholder text. The model then misinterprets `$ARGUMENTS[1]` (the literal text in the second argument definition) as the value of `$ARGUMENTS[0]`.

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
https://github.com/anthropics/claude-code/issues/34164
