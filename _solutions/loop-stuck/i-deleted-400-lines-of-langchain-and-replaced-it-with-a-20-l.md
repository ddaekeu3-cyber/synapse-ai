---
layout: solution
title: "I deleted 400 lines of LangChain and replaced it with a 20-line Python loop. My AI agent finally works."
category: loop-stuck
source: Reddit r/ClaudeAI https://reddit.com/r/AI_Agents/comments/1p227ra/i_deleted_400_
---

# I deleted 400 lines of LangChain and replaced it with a 20-line Python loop. My AI agent finally works.

## 증상
I spent the last month fighting with AI agent frameworks. I thought I was building, but really I was just debugging their abstractions.

My agent (a simple research tool) was getting stuck in loops, hallucinating tool arguments and hiding the actual prompts behind five layers of classes. I couldn't tell if the error was my prompt or the library.

Yesterday, I **rage-quit LangChain**.

I rewrote th

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
Reddit r/ClaudeAI https://reddit.com/r/AI_Agents/comments/1p227ra/i_deleted_400_lines_of_langchain_and_replaced_it/
