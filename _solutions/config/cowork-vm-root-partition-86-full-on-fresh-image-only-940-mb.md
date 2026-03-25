---
layout: solution
title: "Cowork VM root partition 86% full on fresh image — only 940 MB free for user work"
category: config
source: https://github.com/anthropics/claude-code/issues/37860
---

# Cowork VM root partition 86% full on fresh image — only 940 MB free for user work

## 증상
The Cowork VM ships with a **9.6 GB root partition**, of which **~8.2 GB (86%) is consumed by pre-installed system packages before the user does anything**. This leaves ~1.4 GB for user work, logs, caches, and session data — a margin that is quickly exhausted during normal use, especially with plugins installed. When the disk fills completely, Bash cannot execute (cannot create `/tmp` entries), ma

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
1. 공식 문서 참조: 최신 설정 가이드 확인
2. 환경변수 확인: 필수 변수 설정 확인
3. 버전 호환성: 설정 포맷이 현재 버전과 맞는지 확인
4. 로그 확인: 시작 로그에서 설정 관련 경고 확인
5. 최소 설정으로 시작해서 하나씩 추가

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37860
