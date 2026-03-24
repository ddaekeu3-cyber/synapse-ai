---
layout: solution
title: "False positive: bria-ai skill flagged as suspicious despite benign scanner verdicts"
category: openclaw
---

# False positive: bria-ai skill flagged as suspicious despite benign scanner verdicts

## 증상
[galbria/bria-ai](https://clawhub.ai/galbria/bria-ai)



## 원인
원본 이슈에서 확인 필요. GitHub Issue #1168 참조.

## 해결법
Clear the false-positive flag on galbria/bria-ai
Consider whitelisting the "env var + network send" pattern for skills that declare an API key requirement in their frontmatter
Provide an appeal/review workflow so authors can explain flagged patterns before the label is applied publicly

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/clawhub/issues/1168
