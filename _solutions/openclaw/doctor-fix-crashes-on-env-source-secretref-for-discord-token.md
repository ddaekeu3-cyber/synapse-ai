---
layout: solution
title: "doctor --fix crashes on env-source SecretRef for Discord token"
category: openclaw
---

# doctor --fix crashes on env-source SecretRef for Discord token

## 증상
`openclaw doctor --fix` crashes with an unresolved SecretRef error when `channels.discord.accounts.default.token` is configured as an env-source SecretRef. The gateway resolves the token correctly at runtime (Discord is fully functional), but doctor fails before completing checks.

에러 메시지:
```json
{
  "secrets": {
    "providers": {
      "default": {
        "source": "env"
      }
    }
  },
  "channels": {
    "discord": {
      "accounts": {
        "default": {
          "token": {

## 원인
원본 이슈에서 확인 필요. GitHub Issue #50537 참조.

## 해결법
` crashes with an unresolved SecretRef error when `channels.discord.accounts.default.token` is configured as an env-source SecretRef. The gateway resolves the token correctly at runtime (Discord is fully functional), but doctor fails before completing checks.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/50537
