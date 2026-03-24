# Gateway Crashes Every 11 Minutes - Config Backup + Hot Reload Bug

## 증상
Gateway crashes every 11 minutes due to config backup triggering hot reload with lock file cleanup failure.

에러 메시지:
```
Config Backup (11 min) → Updates openclaw.json → Triggers Reload → 
SIGTERM/SIGKILL → Lock File Not Cleaned → Restart Fails
```

## Evidence
**Config Backup Timing** (exactly 11 minutes):
```
01:4

## 원인
원본 이슈에서 확인 필요. GitHub Issue #49188 참조.

## 해결법
es
1. Increase backup interval (11 min → 1 hour)
2. Fix lock file cleanup during reload
3. Add config option: `gateway.reload.enabled: false`

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/49188
