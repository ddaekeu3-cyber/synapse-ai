---
layout: solution
title: "OpenClaw returns 'run Error: LLM Request Failed' on RISC-V64"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/54253
---

# OpenClaw returns "run Error: LLM Request Failed" on RISC-V64

## 증상
OpenClaw fails to make LLM requests on RISC-V64 architecture. Error message: "run Error: LLM Request Failed". Agent cannot function at all on this platform.

## 원인
OpenClaw dependencies may not have RISC-V64 native builds. TLS/crypto libraries or Node.js native addons may be incompatible with the architecture.

## 해결법
### RISC-V64 LLM 요청 실패 해결
1. Node.js가 RISC-V64에서 공식 지원되는 버전인지 확인 (v20+ 권장)
2. `--openssl-no-asm` 플래그로 Node.js 빌드
3. native 모듈 재컴파일: `npm rebuild`
4. 대안: x86_64 Docker 컨테이너에서 QEMU로 실행

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/54253
