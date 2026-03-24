---
layout: solution
title: "sendPolicy deny blocks inbound message processing, not just outbound delivery"
category: general
---

# sendPolicy deny blocks inbound message processing, not just outbound delivery

## 증상
`session.sendPolicy` deny rules block **inbound** message processing, not just outbound delivery. When a deny rule matches (e.g., `{action: "deny", match: {channel: "whatsapp", chatType: "group"}}`), the inbound message is silently dropped — the agent never sees it.



## 원인
원본 이슈에서 확인 필요. GitHub Issue #53328 참조.

## 해결법
In `dispatch-from-config.ts`:
1. Replace the early return block (lines 473-481) with a flag: `const suppressDelivery = sendPolicy === "deny" && !bypassAcpForCommand;`
2. Before the delivery loop (line ~641), check `suppressDelivery` and skip delivery while still returning normally
3. The agent still processes inbound messages via `getReplyFromConfig()`, maintaining context and memory

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: general
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/53328
