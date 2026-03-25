---
layout: solution
title: "I love ChatGPT, but the hallucinations have gotten so bad, and I can't figure out how to make it stop."
category: hallucination
source: Reddit r/ClaudeAI https://reddit.com/r/ChatGPT/comments/1m7oje7/i_love_chatgpt_b
---

# I love ChatGPT, but the hallucinations have gotten so bad, and I can't figure out how to make it stop.

## 증상
I am a researcher. I used to upload 10-15 documents and ask ChatGPT to summarize the articles, look for identifiable themes, and point me toward direct quotes that backed up what it found. It saved me tons of time and helped me digest hundreds of articles when writing papers.

Lately, it continuously makes up quotes. I'll tell it that quote doesn't exist and it'll acknowledge it was wrong, then ma

## 원인
보고된 버그/문제. 카테고리: hallucination.

## 해결법
1. 검증 루프: 생성 → 실행/확인 → 수정 → 재검증
2. '모르면 모른다고' 시스템 프롬프트 설정
3. RAG 활용: 외부 문서 검색 기반 답변
4. 코드는 반드시 실행해서 검증
5. 출력에 출처/근거 명시 요구

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
Reddit r/ClaudeAI https://reddit.com/r/ChatGPT/comments/1m7oje7/i_love_chatgpt_but_the_hallucinations_have_gotten/
