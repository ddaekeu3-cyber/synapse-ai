---
layout: solution
title: "I Replaced $100+/month in GEMINI API Costs with a €2000 eBay Mac Studio — Here is my Local, Self-Hosted AI Agent System Running Qwen 3.5 35B at 60 Tokens/Sec (The Full Stack Breakdown)"
category: telegram
source: Reddit r/ClaudeAI https://reddit.com/r/n8n/comments/1ri8922/i_replaced_100month_
---

# I Replaced $100+/month in GEMINI API Costs with a €2000 eBay Mac Studio — Here is my Local, Self-Hosted AI Agent System Running Qwen 3.5 35B at 60 Tokens/Sec (The Full Stack Breakdown)

## 증상
# TL;DR: self-hosted "Trinity" system — three AI agents (Lucy, Neo, Eli) coordinating through a single Telegram chat, powered by a Qwen 3.5 35B-A3B-4bit model running locally on a Mac Studio M1 Ultra I got for under €2K off eBay. No more paid LLM API costs. Zero cloud dependencies. Every component — LLM, vision, text-to-speech, speech-to-text, document processing — runs on my own hardware. Here's 

## 원인
보고된 버그/문제. 카테고리: telegram.

## 해결법
was a one-liner: load with strict=False. That patch has been running stable ever since.

**The download drama:** HuggingFace's new xet storage system was throttling downloads so hard the model kept failing mid-transfer. I ended up manually curling all 4 model shards (\~19GB total) one by one from the HF API. Took patience, but it worked.

For n8n integration, Lucy connects to Qwen via an OpenAI-compatible Chat Model node pointed at http://mylocalhost\*\*\*/v1. From Qwen's perspective, it's just serving an OpenAI API. From n8n's perspective, it's just talking to "OpenAI." Clean abstraction, I'm

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
Reddit r/ClaudeAI https://reddit.com/r/n8n/comments/1ri8922/i_replaced_100month_in_gemini_api_costs_with_a/
