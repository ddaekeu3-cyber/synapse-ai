---
layout: solution
title: "[Cowork] sandbox.network.allowedDomains ignored — external APIs blocked (api.zotero.org, crossref.org, etc.)"
category: docker
source: https://github.com/anthropics/claude-code/issues/37970
description: "- [x] I have searched existing issues and this hasn't been reported"
---

# [Cowork] sandbox.network.allowedDomains ignored — external APIs blocked (api.zotero.org, crossref.org, etc.)

## 증상
- [x] I have searched [existing issues](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20state%3Aopen%20label%3Abug) and this hasn't been reported yet

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
https://github.com/anthropics/claude-code/issues/37970
