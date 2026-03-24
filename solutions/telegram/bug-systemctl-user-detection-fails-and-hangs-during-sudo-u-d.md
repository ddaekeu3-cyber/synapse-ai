# Bug: systemctl --user detection fails and hangs during `sudo -u` due to SUDO_USER fallback

## 증상
Issue: `openclaw gateway install` always fails with "systemctl --user unavailable: Permission denied" even when systemd user bus is working

에러 메시지:
` — same error, disables the service and can't re-enable it
- Setting `

## 원인
원본 이슈에서 확인 필요. GitHub Issue #44417 참조.

## 해결법
that keeps it running:
```bash

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: telegram
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/44417
