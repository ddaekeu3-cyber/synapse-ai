---
layout: solution
title: "[FEATURE] Mechanism to redact secrets/PII from the context window"
category: context-window
source: https://github.com/anthropics/claude-code/issues/29434
description: "- [x] I have searched existing requests and this feature hasn't been requested"
---

# [FEATURE] Mechanism to redact secrets/PII from the context window

## 증상
- [x] I have searched [existing requests](https://github.com/anthropics/claude-code/issues?q=is%3Aissue+label%3Aenhancement) and this feature hasn't been requested yet

## 원인
Input exceeded the model's maximum context length, causing truncation or a refusal to process the full request. 카테고리: context-window.

## 해결법
from 3rd party tools.
* Builds trust. Customers don't see their secrets/PII in chats. Privacy as first-class citizen.

#### Cons
* Additional maintainence and operational cost on Anthropic.
* Less flexible for customers who may prefer to have their sensitive data in the context
    * Mitigation: 1) Allow turning off the feature with a flag; 2) Today Claude already refuses to pull secret keys, passwords, etc. This might be a non-issue.
 * Sensitive data might still get sent to Anthropic server once
    * Mitigation: Dedicate the server/service for sanitization. Isolate and lock down the server 

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/29434
