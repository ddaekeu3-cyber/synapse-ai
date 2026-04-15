---
layout: solution
title: "Local LLM produces repetitive loops or degenerate output"
category: hallucination
source: MLJourney - Debugging Common Local LLM Errors
description: "Model loops on same phrases or generates nonsense. Output ignores context or contradicts earlier statements. More hallucinations than cloud API"
---

# Local LLM produces repetitive loops or degenerate output

## 증상
Model loops on same phrases or generates nonsense. Output ignores context or contradicts earlier statements. More hallucinations than cloud API versions.

## 원인
Greedy decoding (temperature=0) creating feedback loops. Quantization artifacts corrupting attention patterns. Context window truncation silently cutting information.

## 해결법
### 반복/퇴화 출력 해결

1. **샘플링 조정**:
   ```yaml
   temperature: 0.7      # 0 대신 0.7+
   top_p: 0.9            # nucleus sampling
   repeat_penalty: 1.1   # 반복 페널티
   ```
2. **양자화 레벨 확인**: 4-bit에서 문제 심하면 8-bit으로 올려 테스트
3. **컨텍스트 한도 확인**: 실제 토큰 수가 설정된 윈도우 내인지 모니터링
4. **프롬프트 개선**: "제공된 컨텍스트만 기반으로 답변" 등 명시적 제약 추가
5. **시스템 프롬프트 강화**: "모르면 모른다고 답하라" 지시 포함

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
MLJourney - Debugging Common Local LLM Errors
