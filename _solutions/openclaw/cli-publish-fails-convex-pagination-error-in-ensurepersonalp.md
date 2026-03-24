---
layout: solution
title: "CLI publish fails: Convex pagination error in ensurePersonalPublisherForUser"
category: openclaw
---

# CLI publish fails: Convex pagination error in ensurePersonalPublisherForUser

## 증상
**CLI version:** ClawHub CLI v0.7.0 (8e5ef402)

에러 메시지:
```bash
clawhub login --token <token> --no-browser
# ✔ OK. Logged in as @liveneon.

clawhub --workdir . publish skills/ollama-herd \
  --slug ollama-herd \
  --name "Ollama Herd" \
  --version 1.1.0 \

## 원인
원본 이슈에서 확인 필요. GitHub Issue #1201 참조.

## 해결법
Publishing under an account that has previously published works fine. New accounts are blocked.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/clawhub/issues/1201
