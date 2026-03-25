---
layout: solution
title: "Anyone else stuck in an endless 'Review Code → Improve → Review again' loop with BMAD? (using GLM 4.7)"
category: loop-stuck
source: Reddit r/ClaudeAI https://reddit.com/r/BMAD_Method/comments/1qr9qqh/anyone_else_
---

# Anyone else stuck in an endless "Review Code → Improve → Review again" loop with BMAD? (using GLM 4.7)

## 증상
Hi everyone,  
I've been using BMAD-METHOD with GLM 4.7 and overall I really like the structured workflow and agent-based approach. However, I'm running into a pattern that feels a bit… endless.

My typical flow is:  
- Generate / implement code  
- Run Review Code  
- Apply suggested improvements  
- Run Review Code again  
- Get new suggestions  
- Repeat…

At some point it feels like I'm chasin

## 원인
보고된 버그/문제. 카테고리: loop-stuck.

## 해결법
1. 최대 재시도 제한: 동일 작업 3-5회 제한
2. 에러 패턴 감지: 같은 에러 반복 시 다른 접근법 전환
3. 타임아웃: 단일 작업 시간 제한 설정
4. 상태 체크포인트: 진행상황 기록으로 반복 방지
5. 에스컬레이션: 실패 시 사람에게 보고

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
Reddit r/ClaudeAI https://reddit.com/r/BMAD_Method/comments/1qr9qqh/anyone_else_stuck_in_an_endless_review_code/
