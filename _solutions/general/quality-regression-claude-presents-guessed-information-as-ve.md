---
layout: solution
title: "Quality regression: Claude presents guessed information as verified facts"
category: general
source: https://github.com/anthropics/claude-code/issues/37792
---

# Quality regression: Claude presents guessed information as verified facts

## 증상
When asked about hardware pin assignments (STM32G431KB NUCLEO board), Claude confidently stated specific pin mappings (PA4 = Arduino A2, PA0 = ADC1_IN1) without verifying them against datasheets or documentation. The Arduino header assignment was wrong (PA4 is actually A3, not A2), and the DAC/ADC peripheral assignments were unverified guesses presented as facts.

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
https://github.com/anthropics/claude-code/issues/37792
