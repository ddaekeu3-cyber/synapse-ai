---
layout: solution
title: "EACCES: permission denied on /home/node/.openclaw/identity - Windows 11 Docker Desktop"
category: docker
---

# EACCES: permission denied on /home/node/.openclaw/identity - Windows 11 Docker Desktop

## 증상
On Windows 11 with Docker Desktop, the CLI container cannot create the identity directory due to permission denied errors, making it impossible to use any CLI commands.



## 원인
원본 이슈에서 확인 필요. GitHub Issue #23948 참조.

## 해결법
with mkdir + chown before startup
All approaches fail with same error

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: docker
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/23948
