# Scanner flags env var mismatch because registry summary doesn't read metadata.json env declarations

## 증상
The LLM security scanner (GPT-5-mini) consistently flags skills as "suspicious" when they properly declare environment variables in `metadata.json` but the **registry-level summary** shows "Required env vars: none."



## 원인
원본 이슈에서 확인 필요. GitHub Issue #1053 참조.

## 해결법
Either:
1. **Ingest `env` from metadata.json** into the registry-level summary so the scanner sees consistency, or
2. **Document a supported `env` field** in the SKILL.md frontmatter spec that the registry reads, or
3. **Adjust the scanner prompt** to check metadata.json for env declarations before flagging the mismatch

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: gog
- OS: 다양

## 출처
https://github.com/openclaw/clawhub/issues/1053
