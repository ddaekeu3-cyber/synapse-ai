# Cron job timeout/error should send notification via announce delivery

## 증상
When a cron job times out or fails, the current behavior with `announce` delivery mode is completely silent. The job runs, fails/times out, and no notification is sent to the user — they only discover the failure by checking logs or wondering why expected output never arrived.

에러 메시지:
` fails (times out, errors, etc.), the system should still deliver a notification to the configured channel with:
- Job name
- Error type (timeout, error, etc.)
- Brief reason

Instead of currently: s

## 원인
원본 이슈에서 확인 필요. GitHub Issue #50844 참조.

## 해결법
As a workaround, I've added heartbeat monitoring that checks `cron list` for `consecutiveErrors > 0` and manually notifies the user. But this is not ideal because:
1. It relies on the heartbeat interval (not immediate)
2. It requires custom logic in every agent setup

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/50844
