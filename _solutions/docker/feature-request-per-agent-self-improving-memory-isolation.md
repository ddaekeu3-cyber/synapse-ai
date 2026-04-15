---
layout: solution
title: "Feature request: per-agent self-improving memory isolation"
category: docker
source: https://github.com/openclaw/openclaw/issues/53750
description: "Currently, the self-improving skill stores memory at \\ which resolves to the system home directory. All agents share the same path, meaning their"
---

# Feature request: per-agent self-improving memory isolation

## 증상
Currently, the self-improving skill stores memory at \`~/self-improving/\` which resolves to the system home directory. All agents share the same path, meaning their self-improving memory (corrections, patterns, preferences) is physically mixed together in \`/Users/binary/self-improving/\`.

## 원인
Container permission, networking, or environment variable misconfiguration inside the sandbox.

## 해결법
1. 권한 확인: --user 플래그, 볼륨 권한
2. 네트워크: 컨테이너 간 연결, DNS 확인
3. 로그: docker logs로 에러 확인
4. 리소스 제한: 메모리/CPU 충분한지 확인
5. 볼륨 마운트: 경로 매핑 정확히 확인

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53750
