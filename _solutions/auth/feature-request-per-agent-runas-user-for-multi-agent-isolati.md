---
layout: solution
title: "Feature Request: per-agent runAs user for multi-agent isolation"
category: auth
source: https://github.com/openclaw/openclaw/issues/20033
---

# Feature Request: per-agent runAs user for multi-agent isolation

## 증상
In a multi-agent setup, all agents currently run exec commands as the same OS user (the gateway process owner). This means filesystem isolation between agents is only enforced by convention (SOUL.md instructions), not by actual OS-level permissions.

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
Creating separate Linux users and setting filesystem permissions (chmod 700 on main workspace, 770 with group access for others). However, since the gateway runs all exec calls as the same user, this isolation is not enforced at runtime.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/20033
