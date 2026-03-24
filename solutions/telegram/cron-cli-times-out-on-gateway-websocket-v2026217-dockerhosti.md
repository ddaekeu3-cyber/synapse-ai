# cron CLI times out on gateway WebSocket (v2026.2.17, Docker/Hostinger VPS)

## 증상
`openclaw cron list` (and other cron CLI subcommands) time out after 30s trying to connect to the gateway WebSocket, even though the gateway process is running and `openclaw gateway status` reports "RPC probe: ok".

에러 메시지:
```
Error: gateway timeout after 30000ms
Gateway target: ws://127.0.0.1:18789
Source: local loopback
Config: /data/.openclaw/openclaw.json
Bind: loopback
```

## 원인
원본 이슈에서 확인 필요. GitHub Issue #19874 참조.

## 해결법
Cron jobs continue to execute on schedule. Only the management CLI/API is affected. No workaround found for listing or modifying cron jobs via CLI.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: telegram
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/19874
