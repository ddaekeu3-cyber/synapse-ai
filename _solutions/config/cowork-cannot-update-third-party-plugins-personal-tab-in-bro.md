---
layout: solution
title: "Cowork: Cannot update third-party plugins — Personal tab in Browse Plugins fails to load"
category: config
source: https://github.com/anthropics/claude-code/issues/38185
---

# Cowork: Cannot update third-party plugins — Personal tab in Browse Plugins fails to load

## 증상
Third-party plugins installed via "Add marketplace from GitHub" in Claude Desktop (Cowork) cannot be updated. The **Personal** tab in **Browse plugins** fails to load, making it impossible to access the update/refresh controls for personal marketplace plugins.

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
Re-upload the plugin ZIP manually via Browse plugins → Upload plugin. This is not scalable for end users.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38185
