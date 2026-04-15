---
layout: solution
title: "Skill eligibility checks run on gateway host, not sandbox container — skills blocked despite binaries existing in sandbox`"
category: docker
source: https://github.com/openclaw/openclaw/issues/29254
description: "When the gateway runs inside a Docker container and agents execute in a separate sandbox container, eligibility checks evaluate against the gateway"
---

# Skill eligibility checks run on gateway host, not sandbox container — skills blocked despite binaries existing in sandbox`

## 증상
When the gateway runs inside a Docker container and agents execute in a separate sandbox container, `requires.bins` eligibility checks evaluate against the **gateway container's** `$PATH` instead of the **sandbox container** where skills actually execute. Skills whose required binaries exist in the sandbox image but not in the gateway image are incorrectly marked as blocked and excluded from the a

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
https://github.com/openclaw/openclaw/issues/29254
