---
layout: solution
title: "Windows: cowork-svc.exe burns 17-20% CPU due to per-request Authenticode verification on 1-second polling loop"
category: loop-stuck
source: https://github.com/anthropics/claude-code/issues/31848
---

# Windows: cowork-svc.exe burns 17-20% CPU due to per-request Authenticode verification on 1-second polling loop

## 증상
On Windows, `cowork-svc.exe` (CoworkVMService) continuously consumes 17-20% CPU and ~0.1 MB/s network bandwidth while completely idle. The service log at `C:\ProgramData\Claude\Logs\cowork-service.log` grew to 807K+ lines (512KB) in 4 days.

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
1. API 키 유효성/만료 확인
2. OAuth 토큰 갱신: refresh token 사용
3. 환경변수 확인: .env 파일 설정 검증
4. 캐시된 인증 정보 삭제: `~/.openclaw/credentials.json` 제거 후 재인증
5. IP 화이트리스트/스코프 확인

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/31848
