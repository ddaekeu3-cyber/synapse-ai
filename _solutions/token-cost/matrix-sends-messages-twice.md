---
layout: solution
title: "Matrix sends messages twice"
category: token-cost
source: https://github.com/openclaw/openclaw/issues/49247
description: "Behavior bug (incorrect output/state without"
---

# Matrix sends messages twice

## 증상
Behavior bug (incorrect output/state without crash)

## 원인
GitHub Issue #49247에서 보고된 버그. 해결법이 이슈에 포함됨.

## 해결법
Implement message deduplication based on message_id in the Matrix plugin:

- Cache recent processed message_ids with timestamp
- Before processing, check if message_id exists in cache
- Skip duplicate messages
- Apply deduplication before queuing to prevent queued duplicates

Priority: High (wastes tokens, causes duplicate responses)

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/49247
