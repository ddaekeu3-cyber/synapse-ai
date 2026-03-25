---
layout: solution
title: "Too many RAG chunks cause conflicting information and wrong answers"
category: hallucination
source: Perivitta Rajendran - Why Hallucination Happens
---

# Too many RAG chunks cause conflicting information and wrong answers

## 증상
Inserting 20-50+ document chunks into prompt causes model to give wrong answers. Model blends conflicting sources or prioritizes wrong sections.

## 원인
Context overload with conflicting and repeated information. Model cannot distinguish between relevant and irrelevant chunks when too many are included.

## 해결법
### 컨텍스트 과부하 해결

1. **청크 수 제한**: 후보 많이 검색하되 최종 5-8개만 프롬프트에 포함
2. **리랭킹 필수**: 검색 후 cross-encoder로 관련성 재평가
3. **충돌 감지**: 같은 주제의 상충 정보 자동 감지 → 최신/권위 있는 것 우선
4. **포맷팅**: 핵심 구절을 하이라이트해서 모델이 집중하도록 유도

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
Perivitta Rajendran - Why Hallucination Happens
