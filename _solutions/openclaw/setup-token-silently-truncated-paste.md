---
layout: solution
title: "Setup token silently truncated when terminal line-wraps during paste"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/53464
description: "Pasting Anthropic setup token in terminal appears to work, but authentication fails. Token is silently truncated because terminal wraps long lines and"
---

# Setup token silently truncated when terminal line-wraps during paste

## 증상
Pasting Anthropic setup token in terminal appears to work, but authentication fails. Token is silently truncated because terminal wraps long lines and clipboard paste gets cut off.

## 원인
Terminal line-wrap during paste operation truncates the token string. No validation warns about incorrect token length.

## 해결법
### 설정 토큰 붙여넣기 잘림 해결
1. 환경변수로 설정 (붙여넣기 불필요):
   ```bash
   export ANTHROPIC_API_KEY="sk-..."
   ```
2. 파일로 전달: 토큰을 파일에 저장 후 `cat file | openclaw setup`
3. 터미널 설정: 줄바꿈 없이 긴 줄 표시하도록 설정
4. 토큰 길이 수동 확인: `echo -n "$TOKEN" | wc -c` (예상 길이와 비교)

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53464
