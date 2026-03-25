---
layout: solution
title: "Agent invents non-existent code libraries or false facts"
category: hallucination
source: Stevens Institute + PR-Peri Hallucination Research
---

# Agent invents non-existent code libraries or false facts

## 증상
Agent confidently references libraries, functions, or APIs that do not exist. Generates code using fabricated package names. Provides false information about configurations or settings.

## 원인
LLM probabilistic nature generates plausible-sounding but incorrect information. Without grounding in verified data, the model fills gaps with statistically likely but fictional content.

## 해결법
### 할루시네이션 방지 실전 가이드

1. **검증 루프 구현**
   ```
   생성 → 검증 → 수정 → 재검증
   ```
   - 코드: 생성 후 반드시 실행/컴파일 확인
   - 패키지: `pip show` / `npm ls`로 실존 확인
   - API: 공식 문서와 대조

2. **"모르면 모른다" 시스템 프롬프트**
   ```
   확실하지 않은 정보는 절대 추측하지 마.
   모르면 "확인 필요"라고 명시하고 검색해.
   ```

3. **RAG (Retrieval-Augmented Generation)**
   - 공식 문서를 벡터 DB에 인덱싱
   - 답변 전 관련 문서 검색 → 검색 결과 기반 답변

4. **출력 검증 자동화**
   - import 구문의 패키지명 → 실존 확인
   - URL → HTTP HEAD 요청으로 존재 확인
   - 설정값 → 공식 스키마와 대조

## 예상 토큰 절약
이 에러로 삽질 시: 약 10,000~30,000 토큰 소비
이 해결법 참조 시: 약 2,000 토큰

## 출처
Stevens Institute + PR-Peri Hallucination Research
