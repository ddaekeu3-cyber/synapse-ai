---
layout: solution
title: "Gemini 2.5 Pro thinking content loops infinitely on model switch"
category: loop-stuck
source: https://github.com/openclaw/openclaw/issues/53537
---

# Gemini 2.5 Pro thinking content loops infinitely on model switch

## 증상
Switching to Gemini 2.5 Pro causes agent to enter infinite thinking loop. "Thinking..." indicator never resolves. Agent produces no output.

## 원인
Gemini 2.5 Pro thinking/reasoning content format incompatible with existing message handling. Model switch does not reset thinking state.

## 해결법
### Gemini 모델 전환 루프 해결
1. 모델 전환 전 현재 세션 종료: `/new` 명령으로 새 세션 시작
2. thinking 모드 비활성화: `thinkingMode: false` 설정
3. Gemini 2.5 Pro 대신 Gemini 2.5 Flash 사용 (thinking 문제 없음)
4. 세션 파일 삭제: `~/.openclaw/sessions/` 해당 세션 제거 후 재시작

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53537
