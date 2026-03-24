# Feature Request: Allow configurable thread binding idle timeout (idleHours)

## 증상
Currently, the thread binding `idleHours` defaults to 24 hours, meaning the thread-to-session binding is automatically removed after 24 hours of inactivity. For users who work on personal projects daily and want to continue where they left off, this requires creating a new session each day, which ca



## 원인
원본 이슈에서 확인 필요. GitHub Issue #50204 참조.

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
https://github.com/openclaw/openclaw/issues/50204
