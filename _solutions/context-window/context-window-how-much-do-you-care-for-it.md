---
layout: solution
title: "Context Window; How much do you care for it?"
category: context-window
source: Reddit r/ClaudeAI https://reddit.com/r/GithubCopilot/comments/1rpjujr/context_wi
description: "I've noticed today that Claude model have jumped from 128k to 160k context window limit, I was very happy about it and spent the day working with Sonnet"
---

# Context Window; How much do you care for it?

## 증상
I've noticed today that Claude model have jumped from 128k to 160k context window limit, I was very happy about it and spent the day working with Sonnet 4.6

It was doing well until I felt like it hit a rate limitation, so I decide to try Codex 5.3 again for a prompt. I notice its Context Window is 400k ! That's much larger than Sonnet!

I don't want to get baited and use the wrong model because o

## 원인
Input exceeded the model's maximum context length, causing truncation or a refusal to process the full request. 카테고리: context-window.

## 해결법
something which we all experienced; The model dumbing down for a few hours doesn't mean its now shit. It will be back. 

But noticing that still get me to think, should I prioritize GPT Codex 5.3 over Sonnet 4.6 ?

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
Reddit r/ClaudeAI https://reddit.com/r/GithubCopilot/comments/1rpjujr/context_window_how_much_do_you_care_for_it/
