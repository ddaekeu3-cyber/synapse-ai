---
layout: solution
title: "Feature Request: Agent Spawn Context Hooks — auto-inject knowledge base context on subagent creation"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/52660
---

# Feature Request: Agent Spawn Context Hooks — auto-inject knowledge base context on subagent creation

## 증상
当通过 `sessions_spawn` 创建子会话（subagent/ACP）时，子会话从零开始，无法自动获取已有的经验知识。

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
在子会话的 `AGENTS.md` 中加手动查询步骤，让子会话启动后自行 exec 脚本查知识库。但这有几个问题：
1. 子会话需要先启动、读 AGENTS.md、再查数据库，多一轮来回
2. 不是所有 agent 都能执行 shell 命令（ACP 沙箱可能限制）
3. 关键词提取依赖 agent 自觉，不是框架保证

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/52660
