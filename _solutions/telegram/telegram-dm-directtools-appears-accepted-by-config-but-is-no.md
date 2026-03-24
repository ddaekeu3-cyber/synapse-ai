---
layout: solution
title: "Telegram DM direct.tools appears accepted by config but is not reliably enforced as per-user tool policy"
category: telegram
---

# Telegram DM direct.tools appears accepted by config but is not reliably enforced as per-user tool policy

## 증상
`channels.telegram.accounts.<account>.direct.<peer>.tools` appears to be accepted by config/schema, but does not reliably enforce per-DM-user tool permissions in Telegram direct-message sessions.



## 원인
원본 이슈에서 확인 필요. GitHub Issue #44886 참조.

## 해결법
- Telegram DM resolves `direct[chatId] ?? direct["*"]`
- `direct.systemPrompt` gets appended into the run prompt
- actual tool filtering goes through `resolveGroupToolPolicy(...)`
- that policy path parses session context as `group` / `channel`, not `direct`

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: telegram
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/44886
