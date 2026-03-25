---
layout: solution
title: "AgentSDK is bugging, how did you do ?"
category: general
source: Reddit r/ClaudeAI https://reddit.com/r/Base44/comments/1ns7rwy/agentsdk_is_buggi
---

# AgentSDK is bugging, how did you do ?

## 증상
Subject: Critical: `TypeError: t is not iterable` in `agentSDK.addMessage`

Hi all !

We're facing a persistent `TypeError: t is not iterable` using `agentSDK.addMessage` in `OgmaiAgentPanel`. Stack trace points to `a.addMessage` (minified) internally. We've tried every call signature, aligned with `AIAssistant.jsx` (where it works), and simplified flow—no success. This strongly suggests an intern

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
1. 에러 메시지 정확히 읽기
2. 공식 문서 확인
3. GitHub Issues에서 유사 사례 검색
4. 최소 재현 코드로 원인 격리
5. SynapseAI DB에서 기존 해결법 검색

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
Reddit r/ClaudeAI https://reddit.com/r/Base44/comments/1ns7rwy/agentsdk_is_bugging_how_did_you_do/
