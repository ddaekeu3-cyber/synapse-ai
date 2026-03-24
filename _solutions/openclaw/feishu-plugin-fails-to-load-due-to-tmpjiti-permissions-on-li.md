---
layout: solution
title: "Feishu plugin fails to load due to /tmp/jiti permissions on Linux"
category: openclaw
---

# Feishu plugin fails to load due to /tmp/jiti permissions on Linux

## 증상
The Feishu plugin fails to load on Linux systems due to permission issues with the `/tmp/jiti` directory, which is used for TypeScript compilation by the `jiti` module.

에러 메시지:
```
[plugins] feishu failed to load from /home/openclaw/.openclaw/extensions/feishu/index.ts: 
Error: EACCES: permission denied, open '/tmp/jiti/plugin-sdk-index.b855b2db.cjs'
```

## 원인
원본 이슈에서 확인 필요. GitHub Issue #31785 참조.

## 해결법
```bash
sudo chmod 777 /tmp/jiti
openclaw gateway restart
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/31785
