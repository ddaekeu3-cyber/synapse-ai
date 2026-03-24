# Tool memory_set not found

## 증상
Behavior bug (incorrect output/state without crash)

에러 메시지:
```shell
the session log will produce the log as follows:

>   请把我说的下面一段话记入今天的存储中：我喜欢吃苹果，喜欢泰国，喜欢上海"}],"timestamp":1774146087067}}
{"type":"message","id":"344c26cb","parentId":"2b273513","timestamp":"2

## 원인
원본 이슈에서 확인 필요. GitHub Issue #52033 참조.

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
https://github.com/openclaw/openclaw/issues/52033
