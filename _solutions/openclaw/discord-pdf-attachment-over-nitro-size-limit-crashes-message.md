---
layout: solution
title: "Discord PDF attachment over Nitro size limit crashes message listener"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/47649
---

# Discord PDF attachment over Nitro size limit crashes message listener

## 증상
When a user uploads a PDF file larger than Discord's standard file size limit (8MB), using Discord Nitro to upload files up to 100MB, OpenClaw's message listener for that channel crashes and stops receiving inbound messages.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Upload files to external cloud storage (Google Drive, OneDrive, Dropbox) instead of Discord's attachment system.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/47649
