---
layout: solution
title: "Memory index shows 0 chunks after update, memory_search returns empty"
category: memory
source: https://github.com/openclaw/openclaw/issues/53955
description: "After updating to 2026.3.23-2, memory index shows 0/10 files indexed. memory_search always returns empty results. Memory features completely"
---

# Memory index shows 0 chunks after update, memory_search returns empty

## 증상
After updating to 2026.3.23-2, memory index shows 0/10 files indexed. memory_search always returns empty results. Memory features completely broken.

## 원인
Update changed memory indexing format or path. Existing index incompatible with new version. Migration script not automatically run.

## 해결법
### 메모리 인덱스 0건 해결
1. 메모리 인덱스 재빌드: `openclaw memory reindex`
2. 인덱스 파일 삭제 후 재시작: `rm -rf ~/.openclaw/memory-index/`
3. 메모리 디렉토리 권한 확인: `ls -la ~/.openclaw/memory/`
4. 로그 확인: `openclaw logs --filter memory` 로 구체적 에러 확인

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53955
