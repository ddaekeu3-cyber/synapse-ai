---
layout: solution
title: "nano-banana-pro skill causes duplicate image sends — AI doesn't know OpenClaw auto-attaches MEDIA: output"
category: telegram
---

# nano-banana-pro skill causes duplicate image sends — AI doesn't know OpenClaw auto-attaches MEDIA: output

## 증상
Behavior bug (incorrect output/state without crash)



## 원인
원본 이슈에서 확인 필요. GitHub Issue #52292 참조.

## 해결법
Update the SKILL.md Note to: "The script prints a MEDIA:<path> line — OpenClaw automatically attaches the image to your reply on supported chat providers. **Do NOT send the image again via the message tool.** Just report the saved path to the user." This fix has already been applied locally but the upstream SKILL.md in the openclaw repo also needs updating.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: telegram
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/52292
