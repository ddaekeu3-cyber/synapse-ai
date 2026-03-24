# macOS Talk Mode toggle crashes with Swift exclusivity violation in TalkOverlayView

## 증상
- **OpenClaw App Version:** 2026.3.2 (build 2026030290)

에러 메시지:
```
swift::fatalError(unsigned int, char const*, ...)
swift::runtime::AccessSet::insert(...) / swift_beginAccess
closure #1 in TalkOverlayView.body.getter
protocol witness for View.body.getter in conf

## 원인
원본 이슈에서 확인 필요. GitHub Issue #34630 참조.

## 해결법
Review `TalkOverlayView` for state mutations occurring during `body` evaluation. Ensure any `@State` or `@Binding` modifications happen outside the view body computation, potentially using `DispatchQueue.main.async` or SwiftUI task modifiers.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/34630
