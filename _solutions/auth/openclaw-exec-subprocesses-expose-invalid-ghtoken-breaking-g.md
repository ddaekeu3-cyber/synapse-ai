---
layout: solution
title: "OpenClaw exec subprocesses expose invalid GH_TOKEN, breaking gh even when gh auth login is valid"
category: auth
source: https://github.com/openclaw/openclaw/issues/53709
---

# OpenClaw exec subprocesses expose invalid GH_TOKEN, breaking gh even when gh auth login is valid

## 증상
Regression (worked before, now fails)

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
Use:                                                                                                                       
                                                                                                                            
 ```powershell                                                                                                              
   $env:GH_TOKEN=''; gh ...

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53709
