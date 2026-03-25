---
layout: solution
title: "Is Antigravity down? Possible global outage? “Agent terminated due to error” + stuck loading models"
category: loop-stuck
source: Reddit r/ClaudeAI https://reddit.com/r/google_antigravity/comments/1qjpj33/is_an
---

# Is Antigravity down? Possible global outage? “Agent terminated due to error” + stuck loading models

## 증상
Is anyone else having issues with **A**ntigravity right now?

I was in the middle of a project when it suddenly threw this error: “Agent terminated due to error”

After that, it just got stuck on “loading models” indefinitely. I tried logging out to reset things… and now I can’t log back in at all.  
  
This feels less like a local bug and more like some kind of global outage, but I haven’t seen a

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
Reddit r/ClaudeAI https://reddit.com/r/google_antigravity/comments/1qjpj33/is_antigravity_down_possible_global_outage_agent/
