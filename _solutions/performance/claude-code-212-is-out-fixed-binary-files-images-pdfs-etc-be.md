---
layout: solution
title: "Claude Code 2.1.2 is out - fixed binary files (images, PDFs, etc.) being accidentally included in memory when using"
category: performance
source: Reddit r/ClaudeAI https://reddit.com/r/ClaudeCode/comments/1q7tau9/claude_code_2
---

# Claude Code 2.1.2 is out - fixed binary files (images, PDFs, etc.) being accidentally included in memory when using

## 증상
In the change log, I noticed this single line: this could be the reason for bloating the context, thus hitting the limit faster. 

**• Fixed binary files (images, PDFs, etc.) being accidentally included in memory when using** 

## 원인
보고된 버그/문제. 카테고리: performance.

## 해결법
1. 병목 식별: 프로파일링으로 가장 느린 부분 찾기
2. 캐싱: 반복 연산/API 호출 캐싱
3. 병렬 처리: 독립 작업 동시 실행
4. 타임아웃 설정: 무한 대기 방지
5. 리소스 모니터링: CPU, 메모리, 네트워크 확인

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
Reddit r/ClaudeAI https://reddit.com/r/ClaudeCode/comments/1q7tau9/claude_code_212_is_out_fixed_binary_files_images/
