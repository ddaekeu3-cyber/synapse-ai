---
layout: solution
title: "MC Java - Agent stuck in 'UDP Session Details Received' loop (0B Traffic / IPv6)"
category: loop-stuck
source: Reddit r/ClaudeAI https://reddit.com/r/playit_gg/comments/1rxeaqz/mc_java_agent_
---

# MC Java - Agent stuck in "UDP Session Details Received" loop (0B Traffic / IPv6)

## 증상
Hello guys,

I'm trying to host a Minecraft server (Purpur) on my Mini Server PC, but I'm stuck in a loop and nobody can join. I am also seeing community reports of the service being flagged as malware.

[Client Via Local IP &amp; Tunneled Address](https://preview.redd.it/ng8w6uwawupg1.png?width=1920&amp;format=png&amp;auto=webp&amp;s=599bf82f52c9e79b9b237398a71ce521becb5080)

My server shows up a

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
Reddit r/ClaudeAI https://reddit.com/r/playit_gg/comments/1rxeaqz/mc_java_agent_stuck_in_udp_session_details/
