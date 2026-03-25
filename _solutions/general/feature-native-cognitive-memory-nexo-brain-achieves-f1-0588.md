---
layout: solution
title: "[FEATURE] Native cognitive memory — NEXO Brain achieves F1 0.588 on LoCoMo (+55% vs GPT-4)"
category: general
source: https://github.com/anthropics/claude-code/issues/38337
---

# [FEATURE] Native cognitive memory — NEXO Brain achieves F1 0.588 on LoCoMo (+55% vs GPT-4)

## 증상
Claude Code starts every session from scratch. CLAUDE.md files are manual, static, and grow without bound.

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
We Built

[NEXO Brain](https://github.com/wazionapps/nexo) is an open-source MCP memory server (MIT) that implements the Atkinson-Shiffrin memory model:

- Automatic ingestion from conversations
- Semantic retrieval (768-dim embeddings + BM25 hybrid search)
- Cross-encoder reranking
- Multi-query decomposition for complex questions
- Adaptive Ebbinghaus decay (unique memories protected)
- Dream cycles for overnight consolidation

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38337
