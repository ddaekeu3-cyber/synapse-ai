---
layout: solution
title: "Model ignores in-context rules on first attempt, self-corrects only after failure or user intervention"
category: general
source: https://github.com/anthropics/claude-code/issues/31841
---

# Model ignores in-context rules on first attempt, self-corrects only after failure or user intervention

## 증상
Claude Opus 4.6 consistently fails to apply rules and conventions it demonstrably has in context. Sometimes the model self-corrects after a technical error (e.g., a command fails). Other times, the user must explicitly point out the violation before the model recalls and applies the rule. In both cases, the model can cite the rule afterward — confirming it was in context all along. This pattern pe

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
1. 에러 메시지 정확히 읽기
2. 공식 문서 확인
3. GitHub Issues에서 유사 사례 검색
4. 최소 재현 코드로 원인 격리
5. SynapseAI DB에서 기존 해결법 검색

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/31841
