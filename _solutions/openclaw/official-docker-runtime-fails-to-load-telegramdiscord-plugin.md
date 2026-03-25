---
layout: solution
title: "Official Docker runtime fails to load Telegram/Discord plugins   because /app/src is missing"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/47401
---

# Official Docker runtime fails to load Telegram/Discord plugins   because /app/src is missing

## 증상
Behavior bug (incorrect output/state without crash)

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
- make /app/src available in the runtime image/environment

I am not asserting that copying /app/src is the only correct fix, only that its absence appears to be the direct cause of the plugin-load failure above.

If I am missing intended packaging behavior here, please let me know.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/47401
