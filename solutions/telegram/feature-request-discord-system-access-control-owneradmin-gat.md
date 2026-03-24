# Feature Request: Discord System Access Control (Owner/Admin Gate)

## 증상
When OpenClaw is configured for Discord in a shared server, there's currently no native way to restrict system-level operations (file access, command execution) to specific Discord users. This creates a security risk:

에러 메시지:
```markdown
## Security Rules (USER.md)
- ONLY Daniel (Discord ID: 119510072865980419) has system access
- Everyone else: AI chat only
```

The agent must check sender metadata and refuse system opera

## 원인
원본 이슈에서 확인 필요. GitHub Issue #28137 참조.

## 해결법
Users must manually configure security rules in workspace files:

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: telegram
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/28137
