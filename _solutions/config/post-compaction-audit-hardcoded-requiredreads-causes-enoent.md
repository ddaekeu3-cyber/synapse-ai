---
layout: solution
title: "Post-compaction audit: hardcoded requiredReads causes ENOENT loop when WORKFLOW_AUTO.md doesn't exist"
category: config
source: https://github.com/openclaw/openclaw/issues/33355
description: "After context compaction, the post-compaction audit checks whether the agent read certain \"required startup files.\" The required file list is"
---

# Post-compaction audit: hardcoded requiredReads causes ENOENT loop when WORKFLOW_AUTO.md doesn't exist

## 증상
After context compaction, the post-compaction audit checks whether the agent read certain "required startup files." The required file list is hardcoded:

## 원인
Environment variable, configuration file, or initialization parameter missing, malformed, or incorrectly scoped.

## 해결법
Create a `WORKFLOW_AUTO.md` file in the workspace. This stops the ENOENT but doesn't address the design gap.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/33355
