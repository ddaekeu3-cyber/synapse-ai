---
layout: solution
title: "Memory leak: Missing cleanup for /tmp/claude-*-cwd working directory tracking files"
category: performance
source: https://github.com/anthropics/claude-code/issues/8856
---

# Memory leak: Missing cleanup for /tmp/claude-*-cwd working directory tracking files

## 증상
Claude Code creates temporary files to track working directory changes across Bash command executions but never deletes them, causing accumulation of `/tmp/claude-*-cwd` files.

## 원인
보고된 버그/문제. 카테고리: performance.

## 해결법
Add cleanup immediately after reading the file:

```javascript
// Fixed implementation:
P.result.then(async(k)=>{
  if(k&&!Y&&!k.backgroundTaskId)try{
    let cwdContent=bq6(K,{encoding:"utf8"}).trim();
    try{C1().unlinkSync(K)}catch{};  // <- Add this line
    j$(cwdContent,M)
  }catch{
    B1("tengu_shell_set_cwd",{success:!1})
  }
})
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/8856
