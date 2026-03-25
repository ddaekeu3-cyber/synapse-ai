---
layout: solution
title: "ensureDockerImage() silently overwrites custom sandbox image with plain debian"
category: docker
source: https://github.com/openclaw/openclaw/issues/51185
---

# ensureDockerImage() silently overwrites custom sandbox image with plain debian

## 증상
`ensureDockerImage()` in `dist/reply-Bm8VrLQh.js` has a hardcoded fallback that runs `docker tag debian:bookworm-slim openclaw-sandbox:bookworm-slim` whenever the sandbox image is missing. This silently replaces any custom-built image (with python3, jq, ripgrep, etc.) with plain debian — breaking Write/Edit tools that depend on python3.

## 원인
보고된 버그/문제. 카테고리: docker.

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
https://github.com/openclaw/openclaw/issues/51185
