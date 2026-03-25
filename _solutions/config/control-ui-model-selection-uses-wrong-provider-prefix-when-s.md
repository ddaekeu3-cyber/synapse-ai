---
layout: solution
title: "Control UI: Model selection uses wrong provider prefix when switching models"
category: config
source: https://github.com/openclaw/openclaw/issues/51139
---

# Control UI: Model selection uses wrong provider prefix when switching models

## 증상
Behavior bug (incorrect output/state without crash)

## 원인
GitHub Issue #51139에서 보고된 버그. 해결법이 이슈에 포함됨.

## 해결법
- `moonshot/kimi-k2.5` for Kimi
- `minimax/MiniMax-M2.5` for MiniMax

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51139
