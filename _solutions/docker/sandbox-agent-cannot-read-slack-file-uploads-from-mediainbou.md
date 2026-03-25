---
layout: solution
title: "Sandbox agent cannot read Slack file uploads from media/inbound via read tool"
category: docker
source: https://github.com/openclaw/openclaw/issues/36507
---

# Sandbox agent cannot read Slack file uploads from media/inbound via read tool

## 증상
When files are uploaded via Slack, they are correctly staged to `media/inbound/` in the workspace. The sandbox container can see and read the files via shell (`cat`, `head`), but the agent's `read` file tool consistently fails to access them. The agent tries multiple path variations and eventually gives up.

## 원인
보고된 버그/문제. 카테고리: docker.

## 해결법
Manually copying the file to the project directory works:
```bash
cp ~/.openclaw/workspace-test/media/inbound/<file>.csv ~/.openclaw/workspace-test/test-project/import-data.csv
```

Also, adding a hint to `BOOTSTRAP.md` helps the agent attempt the correct path, but the read tool still fails.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/36507
