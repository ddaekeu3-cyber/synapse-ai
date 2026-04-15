---
layout: solution
title: "I wanted to try Supabase + Cloudflare for a real project — App Store screenshots and icons are always a pain, so I built with Claude Code a Next.js web-based tool to generate them, AI-first."
category: auth
source: Reddit r/ClaudeAI https://reddit.com/r/ClaudeCode/comments/1rtx0r1/i_wanted_to_t
description: "I wanted to share my experience building a full product almost entirely with Claude Code. Not a weekend hack or a demo — a real, deployed, monetized web"
---

# I wanted to try Supabase + Cloudflare for a real project — App Store screenshots and icons are always a pain, so I built with Claude Code a Next.js web-based tool to generate them, AI-first.

## 증상
I wanted to share my experience building a full product almost entirely with Claude Code. Not a weekend hack or a demo — a real, deployed, monetized web app with auth, payments, AI generation, and storage.

**The product:** I'm a mobile developer and I always dread the App Store listing step — screenshots, icons, marketing copy. So I built a tool where you paste a link to your app, AI generates a 

## 원인
Authentication credential mismatch, expiry, or permission scope gap between the requesting agent and the target API.

## 해결법
1. API 키 유효성/만료 확인
2. OAuth 토큰 갱신: refresh token 사용
3. 환경변수 확인: .env 파일 설정 검증
4. 캐시된 인증 정보 삭제: `~/.openclaw/credentials.json` 제거 후 재인증
5. IP 화이트리스트/스코프 확인

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
Reddit r/ClaudeAI https://reddit.com/r/ClaudeCode/comments/1rtx0r1/i_wanted_to_try_supabase_cloudflare_for_a_real/
