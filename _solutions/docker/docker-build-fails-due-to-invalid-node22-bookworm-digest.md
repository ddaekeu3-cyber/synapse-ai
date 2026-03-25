---
layout: solution
title: "Docker build fails due to invalid node:22-bookworm digest"
category: docker
source: https://github.com/openclaw/openclaw/issues/38771
---

# Docker build fails due to invalid node:22-bookworm digest

## 증상
Regression (worked before, now fails)

## 원인
보고된 버그/문제. 카테고리: docker.

## 해결법
Docker.

The build fails during metadata resolution for the base image.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/38771
