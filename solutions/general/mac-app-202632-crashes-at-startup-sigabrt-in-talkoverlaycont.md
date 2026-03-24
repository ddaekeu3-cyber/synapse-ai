# Mac App 2026.3.2 crashes at startup: SIGABRT in TalkOverlayController.present()

## 증상
**Version:** Mac App 2026.3.2 (CFBundleVersion 2026030290)

에러 메시지:
```
swift_beginAccess (exclusive access violation)
closure #1 in TalkOverlayView.body.getter
TalkOverlayController.present()
TalkModeController.setEnabled(_:)
closure #3 in AppState.init(preview:)
```

## 원인
원본 이슈에서 확인 필요. GitHub Issue #35005 참조.

## 해결법
Web dashboard at `127.0.0.1:18789` still works. Gateway runs via LaunchAgent independently.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: general
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/35005
