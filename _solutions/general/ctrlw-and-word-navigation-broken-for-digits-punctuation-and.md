---
layout: solution
title: "Ctrl+W and word navigation broken for digits, punctuation, and non-Latin scripts"
category: general
source: https://github.com/anthropics/claude-code/issues/38354
---

# Ctrl+W and word navigation broken for digits, punctuation, and non-Latin scripts

## 증상
Ctrl+W and other word-based navigation (Alt+Left/Right, Alt+Backspace) only recognize Latin letters as word characters. Digits, punctuation, and non-Latin scripts (Cyrillic, CJK, Arabic, Korean) are treated as non-word filler, causing word deletion to eat through them and into the preceding Latin-letter word.

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
https://github.com/anthropics/claude-code/issues/38354
