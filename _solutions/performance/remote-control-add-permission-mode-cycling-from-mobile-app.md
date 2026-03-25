---
layout: solution
title: "Remote Control: add permission mode cycling from mobile app"
category: performance
source: https://github.com/anthropics/claude-code/issues/29319
---

# Remote Control: add permission mode cycling from mobile app

## 증상
When using Claude Code via Remote Control (`/rc`) from the Claude iOS/web app, there is no way to change the permission mode mid-session. On the CLI terminal, users can press `Shift+Tab` to cycle through permission modes (Default → Accept Edits → Plan → Default), but the mobile Remote Control interface has no equivalent control.

## 원인
보고된 버그/문제. 카테고리: performance.

## 해결법
The user must physically go to the machine running the CLI session and press `Shift+Tab` on the terminal. This defeats the purpose of Remote Control.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/29319
