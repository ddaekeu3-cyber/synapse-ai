# WhatsApp cron delivery always fails with "No active WhatsApp Web listener" despite channel being connected

## 증상
Behavior bug (incorrect output/state without crash)

에러 메시지:
```shell
channels status --probe output (at time of failure):
  WhatsApp default: enabled, configured, linked, running, connected, dm:allowlist

Delivery log (from openclaw logs):
  20:30:28 [cron:JOB

## 원인
원본 이슈에서 확인 필요. GitHub Issue #53162 참조.

## 해결법
the issue:
- openclaw channels login --channel whatsapp --account default (reports "Linked!" but delivery still fails)
- delivery.bestEffort: true (silently swallows error, does not deliver)
- plugins.allow: ["whatsapp"] + gateway restart (no change)
- Multiple gateway restarts

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: gog
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/53162
