---
layout: solution
title: "Fix for 'command 'claude-vscode.editor.openLast' not found' in VS Code Claude extn- 2.1.51"
category: openclaw
source: Reddit r/ClaudeAI https://reddit.com/r/ClaudeAI/comments/1rd9kl1/fix_for_command
---

# Fix for "command 'claude-vscode.editor.openLast' not found" in VS Code Claude extn- 2.1.51

## 증상
If your Claude extension suddenly bricked today and keeps throwing a `command 'claude-vscode.editor.openLast' not found` error every time you try to use it, you aren't alone. It looks like the newest update is bugged and failing to load on startup.

I managed to fix it and get things back to normal by just downgrading the extension to version **2.1.49**.  
If you need a quick workaround while we w

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
1. OpenClaw 최신 버전으로 업데이트: `npm update -g openclaw`
2. Gateway 재시작: `openclaw gateway restart`
3. 설정 파일 확인: `~/.openclaw/config.yaml`
4. 로그 확인: `openclaw logs --tail 50`
5. 원본 GitHub Issue에서 패치 확인

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
Reddit r/ClaudeAI https://reddit.com/r/ClaudeAI/comments/1rd9kl1/fix_for_command_claudevscodeeditoropenlast_not/
