---
layout: solution
title: "My custom n8n workflow for automated bug reporting and common error fixes (with AI agent and Google Sheets database)"
category: general
source: Reddit r/ClaudeAI https://reddit.com/r/n8n/comments/1n116d9/my_custom_n8n_workfl
---

# My custom n8n workflow for automated bug reporting and common error fixes (with AI agent and Google Sheets database)

## 증상
I've created a custom workflow in n8n that automates my bug-fixing process and thought others might find it useful.

Whenever a workflow fails, an Error Trigger fires and extracts all the necessary data—workflow ID, timestamp, node, error message, error stack, execution ID, workflow name and node data(input and output data for all nodes). This data is then logged into a Google Sheet for easy recor

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
Reddit r/ClaudeAI https://reddit.com/r/n8n/comments/1n116d9/my_custom_n8n_workflow_for_automated_bug/
