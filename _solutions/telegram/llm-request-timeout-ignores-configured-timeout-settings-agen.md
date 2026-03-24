---
layout: solution
title: "LLM request timeout ignores configured timeout settings (agent/channel/tool timeouts not respected)"
category: telegram
---

# LLM request timeout ignores configured timeout settings (agent/channel/tool timeouts not respected)

## 증상
Crash (process/app exits or hangs)

에러 메시지:
```shell
Example error:

LLM request timed out

Relevant log lines:

19:10:09 [agent/embedded] embedded run agent end: runId=6e565713-1e9d-470b-840d-36351c61ab1f isError=true model=Donnyed/DeepSeek-R1

## 원인
원본 이슈에서 확인 필요. GitHub Issue #46049 참조.

## 해결법
attempts included increasing all timeout-related configuration values (agents, tools, gateway, browser, plugins, etc.) to extremely large values (86400 seconds / 86400000 ms).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: telegram
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/46049
