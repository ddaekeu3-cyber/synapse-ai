---
layout: solution
title: "Skill invocation produces hallucinated output instead of following skill instructions"
category: hallucination
source: https://github.com/anthropics/claude-code/issues/37890
---

# Skill invocation produces hallucinated output instead of following skill instructions

## 증상
When invoking custom skills (e.g., `/editthispic-cro`), the model ignores the skill's SKILL.md instructions and instead generates completely unrelated hallucinated content — including Anthropic SDK documentation, random conversation snippets, and text containing non-Claude special tokens (`</s>`, `<|end|>`, `<bos>`).

## 원인
보고된 버그/문제. 카테고리: hallucination.

## 해결법
Sending a brief message before invoking the skill (e.g., "hi") prevents the issue. The skill works correctly when it's not the first message in a session.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37890
