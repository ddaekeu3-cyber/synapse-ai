---
layout: solution
title: "Control UI: localStorage settings conflict across different basePath deployments"
category: config
source: https://github.com/openclaw/openclaw/issues/47481
description: "Behavior bug (incorrect output/state without"
---

# Control UI: localStorage settings conflict across different basePath deployments

## 증상
Behavior bug (incorrect output/state without crash)

## 원인
GitHub Issue #47481에서 보고된 버그. 해결법이 이슈에 포함됨.

## 해결법
- Manually clear localStorage when switching between Gateway basePaths
  - Use separate domains/subdomains for each Gateway (not always feasible)

  Proposed fix:
  - Append normalized basePath to localStorage key (e.g., "openclaw.control.settings.v1:/gateway-a")
  - Implement migration logic to preserve existing settings for upgraded users
  - Maintain backward compatibility for root-path deployments (basePath = "")

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/47481
