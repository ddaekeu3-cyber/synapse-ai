---
layout: solution
title: "401 authentication error"
category: auth
source: https://github.com/anthropics/claude-code/issues/30237
---

# 401 authentication error

## 증상
- [x] I have searched [existing issues](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20state%3Aopen%20label%3Abug) and this hasn't been reported yet

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
from previous issues, e.g. https://github.com/anthropics/claude-code/issues/5893#issuecomment-3193488709, but neither work for me.

I'm a Max plan user. My claude code works perfectly as I'm in China using VPN connecting to either Hong Kong or U.S. But it surprise me not working for machines exactly located in U.S.

Other interesting point is that the error message is in Chinese (没有有效的订阅计划, means "No valid subscription plan.") on that U.S. machine. Is claude showing language based on its picture for the user? Does that means that claude is collecting user data and treat user seperately)? :)

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/30237
