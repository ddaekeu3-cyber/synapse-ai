# v2026.3.8: Crash loop on startup — plugin errors, SyntaxErrors, V8 JIT crashes

## 증상
After upgrading from v2026.2.26 to v2026.3.8, the gateway enters a crash loop on startup. **32 crashes in ~50 minutes.** v2026.2.26 was stable (19 crashes over 4 days, mostly cold-start JIT). The gateway is essentially unusable on 2026.3.8.

에러 메시지:
```
[gateway] [plugins] telegram failed to load: TypeError: _zod.z.number(...).int is not a function
```

## 원인
원본 이슈에서 확인 필요. GitHub Issue #41193 참조.

## 해결법
ed in 2026.3.8).

### 3. Telegram plugin — second variant
```
[gateway] [plugins] telegram failed to load: TypeError: Cannot read properties of undefined (reading 'run')
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: telegram
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/41193
