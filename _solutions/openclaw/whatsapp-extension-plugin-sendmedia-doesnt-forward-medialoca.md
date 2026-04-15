---
layout: solution
title: "WhatsApp extension plugin sendMedia doesn't forward mediaLocalRoots (agent workspace media blocked)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/33504
description: "line 298: does not destructure or forward from the outbound context. This causes to receive roots, falling back to defaults that exclude the agent"
---

# WhatsApp extension plugin sendMedia doesn't forward mediaLocalRoots (agent workspace media blocked)

## 증상
`extensions/whatsapp/src/channel.ts` line 298: `sendMedia` does not destructure or forward `mediaLocalRoots` from the outbound context. This causes `assertLocalMediaAllowed` to receive `undefined` roots, falling back to defaults that exclude the agent workspace directory.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
```diff
- sendMedia: async ({ to, text, mediaUrl, accountId, deps, gifPlayback }) => {
+ sendMedia: async ({ to, text, mediaUrl, mediaLocalRoots, accountId, deps, gifPlayback }) => {
    const send = deps?.sendWhatsApp ?? getWhatsAppRuntime().channel.whatsapp.sendMessageWhatsApp;
    const result = await send(to, text, {
      verbose: false,
      mediaUrl,
      accountId: accountId ?? undefined,
      gifPlayback,
+     mediaLocalRoots,
    });
```

The core plugin at `src/channels/plugins/outbound/whatsapp.ts` already has the correct implementation but is unused — the extension plugin is t

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/33504
