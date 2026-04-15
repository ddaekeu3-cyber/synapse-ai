---
layout: solution
title: "MEMORY.md injected twice on Windows NTFS (case-insensitive filesystem)"
category: prompt-engineering
source: https://github.com/openclaw/openclaw/issues/43931
description: "On Windows with WSL2 where the workspace is mounted from a Windows NTFS path (e.g. ), the filesystem is case-insensitive. OpenClaw hardcodes two bootstrap"
---

# MEMORY.md injected twice on Windows NTFS (case-insensitive filesystem)

## 증상
On Windows with WSL2 where the workspace is mounted from a Windows NTFS path (e.g. `D:\openclaw`), the filesystem is case-insensitive. OpenClaw hardcodes two bootstrap patterns for memory:

## 원인
Prompt structure conflict or ambiguous instruction caused the model to misinterpret the intended task. 카테고리: prompt-engineering.

## 해결법
None via `openclaw.json`. Moving workspace to a Linux native filesystem (Docker named volume or WSL2 native path) resolves the issue.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/43931
