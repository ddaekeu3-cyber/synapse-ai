---
layout: solution
title: "Express-session secret regenerated on every container restart, causing infinite login loop"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/29955
---

# Express-session secret regenerated on every container restart, causing infinite login loop

## 증상
The OpenClaw UI becomes permanently inaccessible after any container restart due to the express-session secret being regenerated on startup, which invalidates all browser sessions and causes an infinite login loop.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
may need to come from Hostinger rather than the OpenClaw team, but filing here for visibility.

Proposed fix — persist the session secret across restarts using a file-based approach:

const secretFile = '/data/.openclaw/.express-session-secret';
let secret;
if (fs.existsSync(secretFile)) {
  secret = fs.readFileSync(secretFile, 'utf8').trim();
} else {
  secret = crypto.randomBytes(32).toString('hex');
  fs.writeFileSync(secretFile, secret, { mode: 0o600 });
}

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/29955
