# Discord `allowFrom` Web UI double-escaping IDs

## 증상
Regression (worked before, now fails)

에러 메시지:
```json
"allowFrom": [
  "<userID>"
]
```

### Actual behavior

## Actual behavior

The UI validation rejects unquoted numeric IDs with this error:

```

## 원인
원본 이슈에서 확인 필요. GitHub Issue #52615 참조.

## 해결법
.

## Notes

- The issue appears specifically related to the Web UI form path.
- The desired final stored values are ordinary JSON strings, not strings containing quote characters.
- This may be a regression in the Discord `allowFrom` form widget or schema-driven serialization path.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/52615
