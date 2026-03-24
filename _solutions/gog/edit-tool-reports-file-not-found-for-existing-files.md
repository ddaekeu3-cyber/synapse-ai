---
layout: solution
title: "edit tool reports 'File not found' for existing files"
category: gog
---

# edit tool reports "File not found" for existing files

## 증상
The edit tool reports “File not found” for files that clearly exist and are accessible via other tools (exec, read, write).

에러 메시지:
```shell
[tools] edit failed: File not found: ~/.npm-global/lib/node_modules/openclaw/skills/gog/SKILL.md
```

## 원인
원본 이슈에서 확인 필요. GitHub Issue #30335 참조.

## 해결법
inside the edit tool, or
•	a misleading error message when accessing files outside the workspace root.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: gog
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/30335
