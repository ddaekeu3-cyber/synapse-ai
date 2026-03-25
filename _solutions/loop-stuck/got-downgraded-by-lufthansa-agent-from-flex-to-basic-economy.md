---
layout: solution
title: "Got downgraded by Lufthansa agent from Flex to Basic Economy — now stuck in a reissue loop for 10 days"
category: loop-stuck
source: Reddit r/ClaudeAI https://reddit.com/r/Lufthansa/comments/1otromj/got_downgraded
---

# Got downgraded by Lufthansa agent from Flex to Basic Economy — now stuck in a reissue loop for 10 days

## 증상
I booked a Lufthansa flight from the US to India via Frankfurt with an *Economy Flex* ticket since I anticipated some date changes. I called Lufthansa to change my departure date, and the agent somehow downgraded my fare class to **basic economy** without telling me.

The new ticket they sent still showed “Economy Flex,” but internally it was tagged as a non-flex fare. When I later tried to change

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
Reddit r/ClaudeAI https://reddit.com/r/Lufthansa/comments/1otromj/got_downgraded_by_lufthansa_agent_from_flex_to/
