# macOS Gatekeeper blocks GUI 2026.2.26 launch (code signing / quarantine on bundled files)

## 증상
Upgrading the GUI from 2026.2.24 to 2026.2.26 was blocked by macOS Gatekeeper. The block referenced bundled app content (`prism-bundle.js`), forcing a downgrade to 2.25 and then 2.24. `xattr -cr` also failed with permission denied on the same bundled file. `sudo xattr -cr` was required.

에러 메시지:
` also failed with permission denied on the same bundled file. `

## 원인
원본 이슈에서 확인 필요. GitHub Issue #28141 참조.

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
https://github.com/openclaw/openclaw/issues/28141
