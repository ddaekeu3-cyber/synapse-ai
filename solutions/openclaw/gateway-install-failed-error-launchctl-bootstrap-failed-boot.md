# Gateway install failed: Error: launchctl bootstrap failed: Bootstrap failed: 125: Domain does not support specified action for mac

## 증상
Regression (worked before, now fails)

에러 메시지:
`. The following instructions did not resolve the issue.


### Steps to reproduce

1.install openclaw
2.run openclaw gateway install

### Expected behavior

It can register commands such as OpenClaw g

## 원인
원본 이슈에서 확인 필요. GitHub Issue #46466 참조.

## 해결법
sign in to the macOS desktop as the target user and rerun `openclaw gateway install --force`.
Headless deployments should use a dedicated logged-in user session or a custom LaunchDaemon (not shipped): https://docs.openclaw.ai/gateway

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/46466
