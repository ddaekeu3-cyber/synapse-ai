---
layout: solution
title: "I built a free Singapore FIRE calculator — CPF, SRS, tax, Monte Carlo, 12 withdrawal strategies, and more"
category: auth
source: Reddit r/ClaudeAI https://reddit.com/r/singaporefi/comments/1rcdnl3/i_built_a_fr
---

# I built a free Singapore FIRE calculator — CPF, SRS, tax, Monte Carlo, 12 withdrawal strategies, and more

## 증상
**TL;DR:** I made [sgfireplanner.com](https://sgfireplanner.com), a free Singapore FIRE retirement planner and calculator. It handles CPF (including voluntary top-ups, CPFIS, OA withdrawals, CPF LIFE bequest tracking), SRS, progressive tax, HDB/property, healthcare costs, 12 withdrawal strategies, Monte Carlo simulation, historical backtesting since 1928, and sequence risk stress testing. No login

## 원인
보고된 버그/문제. 카테고리: auth.

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
Reddit r/ClaudeAI https://reddit.com/r/singaporefi/comments/1rcdnl3/i_built_a_free_singapore_fire_calculator_cpf_srs/
