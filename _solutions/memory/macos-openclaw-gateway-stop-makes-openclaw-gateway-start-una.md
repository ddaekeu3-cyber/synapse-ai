---
layout: solution
title: "macOS: `openclaw gateway stop` makes `openclaw gateway start` unable to recover LaunchAgent"
category: memory
source: https://github.com/openclaw/openclaw/issues/53878
---

# macOS: `openclaw gateway stop` makes `openclaw gateway start` unable to recover LaunchAgent

## 증상
Regression (worked before, now fails)

## 원인
GitHub Issue #53878에서 보고된 버그. 해결법이 이슈에 포함됨.

## 해결법
One of these would make behavior match user expectations:

1. Make `openclaw gateway start` bootstrap the existing plist when the LaunchAgent plist exists but the service is not loaded.
2. Or change `openclaw gateway stop` to stop the running process without fully booting out the LaunchAgent from launchd.
3. Or document explicitly that on macOS `stop` requires `install`/bootstrap to restore, though this would still be surprising UX.

Option 1 seems most intuitive.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53878
