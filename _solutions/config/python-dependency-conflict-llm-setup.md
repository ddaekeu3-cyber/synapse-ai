---
layout: solution
title: "Python dependency conflicts prevent LLM framework from running"
category: config
source: MLJourney - Debugging Common Local LLM Errors
---

# Python dependency conflicts prevent LLM framework from running

## 증상
"Module not found" errors, "Incompatible versions" warnings. LLM framework mysteriously fails despite apparently correct installation.

## 원인
Pip allowing incompatible package versions. Accumulated cruft from previous installations. Version drift in requirements across environments.

## 해결법
### Python 의존성 충돌 해결

1. **가상환경 생성** (필수):
   ```bash
   python -m venv llm_env
   source llm_env/bin/activate
   ```
2. **고정 버전 사용**: `requirements.txt`에 정확한 버전 명시
   ```
   transformers==4.35.0
   torch==2.1.0
   ```
3. **클린 설치**: 환경 삭제 → 재생성 → requirements.txt로 설치
4. **충돌 확인**: `pip check` 명령으로 버전 비호환 감지
5. **시스템 패키지 활용**: `apt install python3-torch` 등 (버전 충돌 방지)

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
MLJourney - Debugging Common Local LLM Errors
