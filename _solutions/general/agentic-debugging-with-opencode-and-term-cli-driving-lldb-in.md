---
layout: solution
title: "Agentic debugging with OpenCode and term-cli: driving lldb interactively to chase an ffmpeg/x264 crash (patches submitted)"
category: general
source: Reddit r/ClaudeAI https://reddit.com/r/LocalLLaMA/comments/1qy54sh/agentic_debug
---

# Agentic debugging with OpenCode and term-cli: driving lldb interactively to chase an ffmpeg/x264 crash (patches submitted)

## 증상
Last weekend I built [term-cli](https://github.com/EliasOenal/term-cli), a small tool that gives agents a real terminal (not just a shell). It supports interactive programs like lldb/gdb/pdb, SSH sessions, TUIs, and editors. Anything that would otherwise block an agent. (BSD licensed)

Yesterday I hit a segfault while transcoding with ffmpeg two-pass on macOS. I normally avoid diving into ffmpeg/x

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
1. 에러 메시지 정확히 읽기
2. 공식 문서 확인
3. GitHub Issues에서 유사 사례 검색
4. 최소 재현 코드로 원인 격리
5. SynapseAI DB에서 기존 해결법 검색

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
Reddit r/ClaudeAI https://reddit.com/r/LocalLLaMA/comments/1qy54sh/agentic_debugging_with_opencode_and_termcli/
