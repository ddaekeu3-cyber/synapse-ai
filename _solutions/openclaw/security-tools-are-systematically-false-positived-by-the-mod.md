---
layout: solution
title: "Security tools are systematically false-positived by the moderation pipeline"
category: openclaw
---

# Security tools are systematically false-positived by the moderation pipeline

## 증상
ClawHub's moderation pipeline (static scanner + LLM evaluator) treats security tools identically to potentially malicious skills. Security tools -- scanners, audit tools, IOC databases, threat detectors -- **legitimately contain patterns** that the static scanner flags as suspicious:



## 원인
원본 이슈에서 확인 필요. GitHub Issue #1131 참조.

## 해결법
A **skill category system** where publishers declare their skill's purpose via frontmatter metadata:

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/clawhub/issues/1131
