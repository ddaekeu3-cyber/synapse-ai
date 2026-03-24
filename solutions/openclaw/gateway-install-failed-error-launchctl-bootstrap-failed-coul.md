# Gateway install failed: Error: launchctl bootstrap failed: Could not find domain for user gui: 1000

## 증상
When I run `curl -fsSL https://openclaw.ai/install.sh | bash`  or `openclaw gateway install` it reports the same error `Gateway install failed: Error: launchctl bootstrap failed: Could not find domain for user gui: 1000`. I've tried reboot my mac, and it seems like first time running `curl -fsSL htt

에러 메시지:
` it reports the same error `

## 원인
원본 이슈에서 확인 필요. GitHub Issue #8619 참조.

## 해결법
이 이슈의 해결법은 원본 GitHub Issue를 참조하세요.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/8619
