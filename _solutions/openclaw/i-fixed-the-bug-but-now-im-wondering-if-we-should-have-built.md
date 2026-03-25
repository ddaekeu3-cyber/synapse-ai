---
layout: solution
title: "I fixed the bug. But now I’m wondering if we should have built this agent at all."
category: openclaw
source: Reddit r/ClaudeAI https://reddit.com/r/AIMakeLab/comments/1r75fiq/i_fixed_the_bu
---

# I fixed the bug. But now I’m wondering if we should have built this agent at all.

## 증상
Monday’s “Split Truth” bug is fixed. Pipeline works. Client is happy. Everything’s good.

But I’ve been staring at the logs today and I can’t get past this thought: why are we using an LLM for this?

The task is basically “check if this candidate has 5+ years of experience and matches these 3 skills.” The input is structured data — resume parsers are good enough now that you’re working with fields

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
1. OpenClaw 최신 버전으로 업데이트: `npm update -g openclaw`
2. Gateway 재시작: `openclaw gateway restart`
3. 설정 파일 확인: `~/.openclaw/config.yaml`
4. 로그 확인: `openclaw logs --tail 50`
5. 원본 GitHub Issue에서 패치 확인

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
Reddit r/ClaudeAI https://reddit.com/r/AIMakeLab/comments/1r75fiq/i_fixed_the_bug_but_now_im_wondering_if_we_should/
