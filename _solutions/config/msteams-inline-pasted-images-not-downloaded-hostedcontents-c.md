---
layout: solution
title: "MSTeams: inline pasted images not downloaded (hostedContents contentBytes missing)"
category: config
source: https://github.com/openclaw/openclaw/issues/50043
---

# MSTeams: inline pasted images not downloaded (hostedContents contentBytes missing)

## 증상
Inline pasted images in MSTeams DM and group conversations are not downloaded. The Teams Graph API returns `hostedContents` without `contentBytes`, so the current download logic silently fails.

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
is to download the image via the `contentUrl` using the bot's auth token instead of relying on the inline content bytes.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50043
