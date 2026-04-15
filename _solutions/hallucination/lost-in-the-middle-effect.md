---
layout: solution
title: "LLM ignores information placed in the middle of long prompts"
category: hallucination
source: Perivitta Rajendran - Why Hallucination Happens
description: "Agent misses critical information that is placed in the middle of the prompt. Answers based only on beginning/end of context, ignoring middle"
---

# LLM ignores information placed in the middle of long prompts

## 증상
Agent misses critical information that is placed in the middle of the prompt. Answers based only on beginning/end of context, ignoring middle sections.

## 원인
Lost-in-the-middle effect: LLMs perform best when relevant information appears near the beginning or end of the prompt. Information placed in the middle is often overlooked.

## 해결법
### "Lost in the Middle" 효과 해결

1. **핵심 정보 위치 전략**
   - 가장 중요한 정보 → 프롬프트 시작 부분
   - 두 번째로 중요한 → 프롬프트 끝 부분
   - 보조 정보만 중간에 배치

2. **청크 수 최소화**: 전송 청크 수를 줄여 중간 영역 자체를 축소

3. **명시적 포맷팅**
   ```
   === CRITICAL INFORMATION ===
   [핵심 정보]
   === END CRITICAL ===
   ```

4. **분할 질의**: 긴 프롬프트 대신 여러 짧은 질의로 분할

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
Perivitta Rajendran - Why Hallucination Happens
