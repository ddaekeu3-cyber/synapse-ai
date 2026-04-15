---
layout: solution
title: "CoworkVMService crashes with 'Incorrect function' on Windows 11 Pro 25H2 Build 26200 (v1.1.5749)"
category: general
source: https://github.com/anthropics/claude-code/issues/32481
description: "CoworkVMService immediately crashes with \"Incorrect function\" (service-specific error) on Windows 11 Pro 25H2 Build 26200. The VM bundle downloads"
---

# CoworkVMService crashes with 'Incorrect function' on Windows 11 Pro 25H2 Build 26200 (v1.1.5749)

## 증상
CoworkVMService immediately crashes with "Incorrect function" (service-specific error) on Windows 11 Pro 25H2 Build 26200. The VM bundle downloads successfully ("All files ready") but the service binary (`cowork-svc.exe`) fails to initialize.

## 원인
Agent encountered an unexpected state or unhandled error condition outside the standard error handling path.

## 해결법
changing TEMP to C:\Users\...\AppData\Local\Temp.
- The MSIX sandbox complicates debugging — `cowork-svc.exe` cannot be launched directly ("Access is denied") and `sc.exe config` also returns "Access denied" for this MSIX-registered service.
- The service crashes **before** any HCS/network operations — "Incorrect function" appears to be during early initialization (possibly signature verification or path resolution).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/32481
