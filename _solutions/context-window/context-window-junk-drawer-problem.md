---
layout: solution
title: "Context window used as 'junk drawer' — everything dumped inside"
category: context-window
source: LogRocket Blog - The LLM Context Problem in 2026
description: "Agent performance degrades because context window is filled with irrelevant information. Important instructions get diluted among noise. Token costs"
---

# Context window used as "junk drawer" — everything dumped inside

## 증상
Agent performance degrades because context window is filled with irrelevant information. Important instructions get diluted among noise. Token costs inflate unnecessarily.

## 원인
Teams treating context windows like junk drawers, dumping everything inside without curation. No information discipline applied to what goes into context.

## 해결법
### 컨텍스트 정리 전략

1. **정보 분류 체계**:
   - 필수: 시스템 프롬프트, 현재 태스크, 핵심 제약조건
   - 선택: 관련 예시, 이전 결정
   - 제외: 이전 실패 시도, 무관한 대화, 중복 정보

2. **동적 컨텍스트 로딩**: 태스크에 필요한 정보만 선택적 로드
   ```
   if task_type == "code_review":
       load(file_content, style_guide)  # 코드 + 스타일만
   elif task_type == "bug_fix":
       load(error_log, relevant_code, test_results)  # 에러 관련만
   ```

3. **토큰 예산 설정**: 컨텍스트 영역별 토큰 한도 지정
4. **정기적 감사**: 컨텍스트에 들어가는 내용 주기적 검토

## 예상 토큰 절약
이 에러로 삽질 시: 약 15,000~40,000 토큰 소비
이 해결법 참조 시: 약 2,000 토큰

## 출처
LogRocket Blog - The LLM Context Problem in 2026
