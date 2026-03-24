# [Feature]: heartbeat.timeoutSeconds — per-heartbeat embedded run timeout

## 증상
Heartbeat embedded runs inherit agents.defaults.timeoutSeconds (default 600s). There is no way to set a shorter timeout specifically for heartbeats. Heartbeats are lightweight status checks that should fail fast (30-60s), not block for 10 minutes when a model hangs.

에러 메시지:
```
{
  agents: {
    defaults: {
      heartbeat: {
        every: "30m",
        timeoutSeconds: 60,  // NEW — override agents.defaults.timeoutSeconds for heartbeat runs
      }
    }
  }
}
```

Pre

## 원인
원본 이슈에서 확인 필요. GitHub Issue #47456 참조.

## 해결법
Add heartbeat.timeoutSeconds at both agents.defaults.heartbeat and agents.list[].heartbeat level:

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: gog
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/47456
