---
layout: solution
title: "CLAUDE.md self-improvement instructions not consistently followed by the model"
category: config
source: https://github.com/anthropics/claude-code/issues/38178
---

# CLAUDE.md self-improvement instructions not consistently followed by the model

## 증상
Claude Code does not consistently apply self-improvement instructions defined in `CLAUDE.md`, even when they are explicit and well-structured.

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
works (build, type-check)
3. **Proactively update the relevant rule/memory** so the mistake doesn't repeat

The instruction was explicit: *"Ne pas demander la permission pour ces mises à jour — les faire silencieusement dans le cadre du fix. L'utilisateur ne devrait jamais avoir à dire 'crée une rule pour ça'"* (Don't ask permission — do it silently. The user should never have to say "create a rule for this").

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38178
