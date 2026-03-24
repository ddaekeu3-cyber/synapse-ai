---
layout: solution
title: "HostGuard flagged as Suspicious despite 0/64 VirusTotal detections"
category: openclaw
---

# HostGuard flagged as Suspicious despite 0/64 VirusTotal detections

## 증상
My skill `hostguard` (v1.0.0) is showing a "Suspicious patterns detected" warning



## 원인
원본 이슈에서 확인 필요. GitHub Issue #1081 참조.

## 해결법
with `.env` backup before any modification

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/clawhub/issues/1081
