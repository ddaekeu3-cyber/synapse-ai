---
layout: solution
title: "Remote control: image base64 data arrives empty, causing API 400 error"
category: general
source: https://github.com/anthropics/claude-code/issues/34338
---

# Remote control: image base64 data arrives empty, causing API 400 error

## 증상
When using Claude Code via **remote control** (Claude app UI connecting to a CLI instance), images attached to messages arrive with empty base64 data. The image content block structure is preserved (`type: "image"`, `source.type: "base64"`, `media_type: "image/png"`), but `source.base64` is an empty string.

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
Save images to a synced folder and use the `Read` tool to view them, or use a separate web UI that handles images via direct file upload.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/34338
