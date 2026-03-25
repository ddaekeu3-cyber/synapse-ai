---
layout: solution
title: "[Feature]: Memory indexing lacks checkpoint/resume capability for long-running operations"
category: memory
source: https://github.com/openclaw/openclaw/issues/26772
---

# [Feature]: Memory indexing lacks checkpoint/resume capability for long-running operations

## 증상
The memory indexer uses an atomic temp-file-swap pattern that loses ALL progress if the process crashes during indexing. For large codebases requiring hours to index, this is a critical reliability issue.

## 원인
보고된 버그/문제. 카테고리: memory.

## 해결법
None. Users must:
- Hope for 7-9 hour crash-free operation
- Manually babysit the process
- Accept complete restarts on any failure

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/26772
