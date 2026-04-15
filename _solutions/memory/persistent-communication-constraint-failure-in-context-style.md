---
layout: solution
title: "Persistent communication constraint failure: in-context style rules degrade over session length despite persistent memory"
category: memory
source: https://github.com/anthropics/claude-code/issues/31611
description: "Reporter: Robert Bishop, Ann Arbor,"
---

# Persistent communication constraint failure: in-context style rules degrade over session length despite persistent memory

## 증상
**Reporter:** Robert Bishop, Ann Arbor, Michigan

## 원인
Agent session state was not persisted to durable storage, causing context to be lost on restart or session switch.

## 해결법
it. That is becoming evident as a fatal flaw in the functioning of the large language model being used."

The user is correct that this represents a structural limitation: explicit in-context instruction-following for communication style constraints degrades predictably over session length, even when those constraints are written into persistent memory files, agreed explicitly, and tied to clear rationale.

---

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/31611
