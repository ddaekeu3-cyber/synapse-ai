---
layout: solution
title: "Refund not working and stuck in a loop with the AI agent"
category: loop-stuck
source: Reddit r/ClaudeAI https://reddit.com/r/paypal/comments/1r66jge/refund_not_workin
---

# Refund not working and stuck in a loop with the AI agent

## 증상
Was due a refund at the end of December, it briefly showed in my PayPal account but never appeared in my bank account despite them claiming it was sent. The refunding company is still able to take money so find it quite curious that they can do that but not send a refund.

I got in a stupid loop with the AI agency because it was so confused by what I was saying. Got referred to a real person via e

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
Reddit r/ClaudeAI https://reddit.com/r/paypal/comments/1r66jge/refund_not_working_and_stuck_in_a_loop_with_the/
