---
layout: solution
title: "Edit tool changes to git-tracked files silently reverted during context compaction"
category: performance
source: https://github.com/anthropics/claude-code/issues/34674
---

# Edit tool changes to git-tracked files silently reverted during context compaction

## 증상
When a long conversation triggers context compaction (message compression), uncommitted changes made via the Edit tool to git-tracked files are silently reverted to their `git HEAD` state. New files created with the Write tool are not affected.

## 원인
보고된 버그/문제. 카테고리: performance.

## 해결법
Commit immediately after every Edit-tool change:
```bash
git add <file> && git commit -m "description"
```

Or for multi-file atomic changes, write a Node.js script to `/tmp/` using `fs.writeFileSync`, then chain:
```bash
node /tmp/fix.js && git add <files> && git commit -m "description"
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/34674
