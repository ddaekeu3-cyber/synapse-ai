---
layout: solution
title: "MS Teams: graph media fetch empty for channel file attachments despite valid permissions and token"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/51749
---

# MS Teams: graph media fetch empty for channel file attachments despite valid permissions and token

## 증상
MS Teams channel file attachments (PDFs, documents) are never delivered to the agent. The plugin logs `graph media fetch empty` on every inbound file, despite all Graph API permissions being correctly configured and working when tested manually.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
None found. Users can paste URLs instead of uploading files as a temporary workaround.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51749
