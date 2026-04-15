---
layout: solution
title: "Feature: skills.descriptionMode for token-efficient skill injection"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/31206
description: "OpenClaw injects all skill descriptions into the system prompt every session. With 70+ skills, descriptions consume ~1,100-3,400 tokens depending on"
---

# Feature: skills.descriptionMode for token-efficient skill injection

## 증상
OpenClaw injects all skill descriptions into the system prompt every session. With 70+ skills, descriptions consume ~1,100-3,400 tokens depending on verbosity — most of which are wasted because only 1-2 skills are used per session.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Users can manually compress skill descriptions in `~/.openclaw/skills/*/SKILL.md` frontmatter. I compressed 69 skills from ~2,575 to ~823 words (68% reduction). But this is fragile — `clawhub install` or skill updates will overwrite the compression.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/31206
