---
layout: solution
title: "WhatsApp plugin broken after upgrading to 2026.3.22"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/53216
description: "Version: 2026.3.22 (downgraded back to 2026.3.13 as"
---

# WhatsApp plugin broken after upgrading to 2026.3.22

## 증상
**Version:** 2026.3.22 (downgraded back to 2026.3.13 as workaround)

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Downgrade to 2026.3.13 restores full functionality.

---

Happy to provide logs or additional details if helpful.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53216
