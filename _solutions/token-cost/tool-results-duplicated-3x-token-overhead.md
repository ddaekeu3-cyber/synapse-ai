---
layout: solution
title: "Tool results duplicated in session context — 3x token overhead"
category: token-cost
source: https://github.com/openclaw/openclaw/issues/53803
---

# Tool results duplicated in session context — 3x token overhead

## 증상
Tool call results are stored 3 times in session context when using OmniRoute gateway. Token usage is 3x expected. Costs triple for every tool-using conversation.

## 원인
OmniRoute gateway duplicates tool results in the message history. Each tool response appears in: original response, session history, and gateway cache.

## 해결법
### 도구 결과 중복 저장 해결
1. OmniRoute 설정에서 `deduplicateToolResults: true` 활성화
2. 세션 히스토리 정리: 중복 tool_result 메시지 제거 스크립트 실행
3. 대안: OmniRoute 대신 직접 provider 연결
4. 임시 해결: 세션 길이를 짧게 유지하여 중복 누적 방지

## 예상 토큰 절약
이 에러로 삽질 시: 약 30,000~90,000 토큰 소비
이 해결법 참조 시: 약 1,000 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53803
