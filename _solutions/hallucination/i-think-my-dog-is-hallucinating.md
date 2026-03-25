---
layout: solution
title: "I think my dog is hallucinating"
category: hallucination
source: Reddit r/ClaudeAI https://reddit.com/r/WhatsWrongWithYourDog/comments/1ntx155/i_
---

# I think my dog is hallucinating

## 증상
For context, there’s 4 dogs and 3 people, no one else hears anything. This is a house and we’re in the basement, so no upstairs neighbours. 

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
Reddit r/ClaudeAI https://reddit.com/r/WhatsWrongWithYourDog/comments/1ntx155/i_think_my_dog_is_hallucinating/
