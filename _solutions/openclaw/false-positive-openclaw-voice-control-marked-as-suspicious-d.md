---
layout: solution
title: "[False Positive] openclaw-voice-control marked as Suspicious despite 0/42 VirusTotal detections"
category: openclaw
---

# [False Positive] openclaw-voice-control marked as Suspicious despite 0/42 VirusTotal detections

## 증상
openclaw-voice-control is currently marked as Suspicious. Based on the scan output, the likely reason is the presence of higher-risk setup patterns such as external repository cloning, shell script execution, launchd configuration, and use of locally provided configuration or credentials.



## 원인
원본 이슈에서 확인 필요. GitHub Issue #1186 참조.

## 해결법
이 이슈의 해결법은 원본 GitHub Issue를 참조하세요.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/clawhub/issues/1186
