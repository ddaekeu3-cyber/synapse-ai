---
layout: solution
title: "CLAUDE.md commit conventions overridden by system prompt defaults"
category: prompt-engineering
source: https://github.com/anthropics/claude-code/issues/31776
---

# CLAUDE.md commit conventions overridden by system prompt defaults

## 증상
Project-level CLAUDE.md commit conventions are being ignored in favor of the built-in system prompt commit template, even after CLAUDE.md has been explicitly read in the same session.

## 원인
보고된 버그/문제. 카테고리: prompt-engineering.

## 해결법
1. 명확한 지시: 구체적이고 명확한 표현
2. Few-shot 예시: 원하는 출력 예시 제공
3. 역할 지정: 시스템 프롬프트에 역할/제약 명시
4. 출력 포맷 지정: JSON, 마크다운 등
5. 보안: 프롬프트 인젝션 방지 입력 검증

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/31776
