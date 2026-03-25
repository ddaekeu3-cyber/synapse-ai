---
layout: solution
title: "Discord thread-bound ACP child replies appear to be mirrored into the main Discord route transcript instead of the child session transcript"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/42789
---

# Discord thread-bound ACP child replies appear to be mirrored into the main Discord route transcript instead of the child session transcript

## 증상
On a pure OpenClaw VPS deployment, Discord thread-bound ACP sessions are usable end-to-end, but the child session transcript/store projection appears to land in the main Discord route transcript instead of the ACP child session transcript.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
We can continue to use Discord ACP in practice, but operators have to treat the main Discord route transcript as a fallback observation path when the child session summary/store are empty.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/42789
