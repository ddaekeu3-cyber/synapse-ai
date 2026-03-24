---
layout: solution
title: "Feature Request: Filesystem Sandboxing Config (tools.fileAccess)"
category: docker
---

# Feature Request: Filesystem Sandboxing Config (tools.fileAccess)

## 증상
**Feature:** Filesystem access restrictions via configuration

에러 메시지:
```javascript
{
  "tools": {
    "fileAccess": {
      "allowedPaths": ["/home/seraph/.openclaw/workspace", "/tmp"],
      "denyPaths": ["/etc", "/root", "~/.ssh", "/var/log"]
    }
  }
}
```

**Resul

## 원인
원본 이슈에서 확인 필요. GitHub Issue #7722 참조.

## 해결법
**
None. We're relying on OS-level user permissions (running as non-root `seraph` user).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: docker
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/7722
