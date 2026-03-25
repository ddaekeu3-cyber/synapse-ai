---
layout: solution
title: "Google should really implement a feature that tells if the agent stuck or looping"
category: loop-stuck
source: Reddit r/ClaudeAI https://reddit.com/r/google_antigravity/comments/1rjjai3/googl
---

# Google should really implement a feature that tells if the agent stuck or looping

## 증상
https://preview.redd.it/4ic5i5ahbsmg1.png?width=507&amp;format=png&amp;auto=webp&amp;s=1a122091de697292f0042b80e73c0495b389c87e

  
its been like this for a while and i never truly know if it is still working and generating internally or whether it got actually stuck and I need to cancel the process. Like you, you have no knowledge of what's going on in these stages. 

## 원인
보고된 버그/문제. 카테고리: loop-stuck.

## 해결법
1. 최대 재시도 제한: 동일 작업 3-5회 제한
2. 에러 패턴 감지: 같은 에러 반복 시 다른 접근법 전환
3. 타임아웃: 단일 작업 시간 제한 설정
4. 상태 체크포인트: 진행상황 기록으로 반복 방지
5. 에스컬레이션: 실패 시 사람에게 보고

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
Reddit r/ClaudeAI https://reddit.com/r/google_antigravity/comments/1rjjai3/google_should_really_implement_a_feature_that/
