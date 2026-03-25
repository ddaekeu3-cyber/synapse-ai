---
layout: solution
title: "LSP tool hangs indefinitely when server doesn't respond to a request"
category: performance
source: https://github.com/anthropics/claude-code/issues/29728
---

# LSP tool hangs indefinitely when server doesn't respond to a request

## 증상
When an LSP server silently drops a request (no response, no error), the LSP tool blocks forever with no timeout, making the entire session unrecoverable without force-quitting. Repro: Ruby LSP receives `textDocument/prepareCallHierarchy` (which it doesn't support), matches no case branch in its message router, and sends nothing back. Claude Code's debug log shows `Sending request 'textDocument/pr

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
https://github.com/anthropics/claude-code/issues/29728
