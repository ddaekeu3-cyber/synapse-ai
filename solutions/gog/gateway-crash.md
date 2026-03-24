# Gateway crash

## 증상
Crash (process/app exits or hangs)

에러 메시지:
```shell
Mar 16 06:59:48 j-openclaw systemd[938]: Stopping openclaw-gateway.service...
Mar 16 06:59:48 j-openclaw node[317829]: 2026-03-16T06:59:48.508+08:00 [gateway] signal SIGTERM received
Mar 16 0

## 원인
원본 이슈에서 확인 필요. GitHub Issue #47746 참조.

## 해결법
Manually kill orphan processes with kill -9 $(lsof -t -i:18789)

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: gog
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/47746
