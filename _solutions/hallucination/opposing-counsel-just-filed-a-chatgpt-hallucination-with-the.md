---
layout: solution
title: "Opposing Counsel Just Filed a ChatGPT Hallucination with the Court"
category: hallucination
source: Reddit r/ClaudeAI https://reddit.com/r/ChatGPT/comments/1n7ucjj/opposing_counsel
---

# Opposing Counsel Just Filed a ChatGPT Hallucination with the Court

## 증상
TLDR; opposing counsel just filed a brief that is 100% an AI hallucination. The hearing is on Tuesday.

I'm an attorney practicing civil litigation. Without going to far into it, we represent a client who has been sued over a commercial licensing agreement. Opposing counsel is a collections firm. Definitely not very tech-savvy, and generally they just try their best to keep their heads above water

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
Reddit r/ClaudeAI https://reddit.com/r/ChatGPT/comments/1n7ucjj/opposing_counsel_just_filed_a_chatgpt/
