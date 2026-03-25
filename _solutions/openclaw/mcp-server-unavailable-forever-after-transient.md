---
layout: solution
title: "MCP server stays unavailable forever after transient startup failure"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/53864
---

# MCP server stays unavailable forever after transient startup failure

## 증상
MCP server encounters a brief startup failure (network timeout, etc). After the transient error resolves, the MCP server remains marked as "unavailable" indefinitely. Requires full gateway restart.

## 원인
No retry/recovery mechanism for MCP server initialization. Once marked unavailable, status is never rechecked. Existing sessions cannot reconnect.

## 해결법
### MCP 서버 영구 불가용 해결
1. Gateway 재시작: `openclaw gateway restart`
2. MCP 서버 상태 수동 리셋: MCP 설정에서 서버 제거 후 재추가
3. 자동 복구 설정 (지원 시): healthcheck 간격을 설정
4. 임시 해결: 새 세션을 시작하면 MCP 서버 재연결 시도

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53864
