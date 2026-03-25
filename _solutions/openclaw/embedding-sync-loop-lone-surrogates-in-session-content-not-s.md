---
layout: solution
title: "Embedding sync loop — lone surrogates in session content not sanitized before sending to embeddings API"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/27753
---

# Embedding sync loop — lone surrogates in session content not sanitized before sending to embeddings API

## 증상
When session content contains a lone Unicode surrogate character, OpenClaw's embedding pipeline sends it unsanitized to the embeddings provider. The provider rejects it with a 500 error, which gets stored in session state, re-embedded on the next sync cycle, fails again — creating a permanent loop with no auto-recovery.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
1. OpenClaw 최신 버전으로 업데이트: `npm update -g openclaw`
2. Gateway 재시작: `openclaw gateway restart`
3. 설정 파일 확인: `~/.openclaw/config.yaml`
4. 로그 확인: `openclaw logs --tail 50`
5. 원본 GitHub Issue에서 패치 확인

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/27753
