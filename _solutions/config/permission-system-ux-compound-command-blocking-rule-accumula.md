---
layout: solution
title: "Permission system UX: compound command blocking, rule accumulation, undiscoverable Bash(*) fix"
category: config
source: https://github.com/anthropics/claude-code/issues/31523
---

# Permission system UX: compound command blocking, rule accumulation, undiscoverable Bash(*) fix

## 증상
The permission system has a compounding UX problem that makes "autonomous" agent usage impractical for long-term users. After 3 months of daily use, I accumulated **150+ narrow permission rules** in `settings.local.json` — and compound commands (`cd /my/project && git status`) **still prompted for approval** every time because they don't match the individually-allowed patterns.

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
(Trivial but Undiscoverable)

```json
{
  "permissions": {
    "allow": ["Bash(*)", "Read(*)", "Write(*)", "Edit(*)", "Glob(*)", "Grep(*)"],
    "ask": ["Bash(rm -rf:*)", "Bash(git push --force:*)", "Bash(git reset --hard:*)"]
  }
}
```

8 rules replace 150+. Dangerous commands still prompt. Everything else just works. Real safety comes from hooks (RAM auditing, pre-commit validation), not permission prompts on `ls`.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/31523
