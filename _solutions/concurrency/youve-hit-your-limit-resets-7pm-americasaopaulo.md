---
layout: solution
title: "You've hit your limit · resets 7pm (America/Sao_Paulo)"
category: concurrency
source: https://github.com/anthropics/claude-code/issues/37906
---

# You've hit your limit · resets 7pm (America/Sao_Paulo)

## 증상
I’m carrying out my usual development tasks, but my credits ran out twice at an unusually fast rate. It doesn’t make sense that I would have used all the tokens in such a short time.

## 원인
보고된 버그/문제. 카테고리: concurrency.

## 해결법
1. 락 사용: 공유 리소스에 적절한 락/뮤텍스
2. 원자적 연산: 경쟁 조건 방지
3. 큐 기반 처리: 메시지 큐로 통신
4. 타임아웃: 락 대기에 타임아웃 설정
5. 스트레스 테스트: 동시성 버그 발견

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37906
