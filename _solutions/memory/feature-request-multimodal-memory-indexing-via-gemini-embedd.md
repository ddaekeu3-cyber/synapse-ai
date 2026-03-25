---
layout: solution
title: "Feature request: Multimodal memory indexing via Gemini Embedding 2"
category: memory
source: https://github.com/openclaw/openclaw/issues/47651
---

# Feature request: Multimodal memory indexing via Gemini Embedding 2

## 증상
Support indexing of non-Markdown media files (images, video, audio, PDFs) in the memory search pipeline using Gemini Embedding 2's native multimodal capabilities.

## 원인
보고된 버그/문제. 카테고리: memory.

## 해결법
The agent manually summarises media into a Markdown note → that text summary is embedded. One extra step and loses fidelity vs. embedding the raw media directly.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/47651
