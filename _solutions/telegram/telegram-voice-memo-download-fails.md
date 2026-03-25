---
layout: solution
title: "Telegram Voice Memo Download Fails"
category: telegram
source: https://github.com/openclaw/openclaw/issues/44747
---

# Telegram Voice Memo Download Fails

## 증상
Behavior bug (incorrect output/state without crash)

## 원인
보고된 버그/문제. 카테고리: telegram.

## 해결법
echo "149.154.166.110 api.telegram.org" | sudo tee -a /etc/hosts
openclaw gateway restart

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/44747
