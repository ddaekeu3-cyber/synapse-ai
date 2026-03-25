---
layout: solution
title: "read_file tool usage error (seems to be a bug?)"
category: general
source: Reddit r/ClaudeAI https://reddit.com/r/RooCode/comments/1n6ip09/read_file_tool_u
---

# read_file tool usage error (seems to be a bug?)

## 증상
I'm having problem getting my agent to use the correct read\_file tool format, by looking at the chat history:

&lt;read\_file&gt;  
&lt;args&gt;  
  &lt;file&gt;  
&lt;path&gt;src/main/host/host.rs&lt;/path&gt;  
&lt;line\_range&gt;790-810&lt;/line\_range&gt;  
  &lt;/file&gt;  
&lt;/args&gt;  
&lt;/read\_file&gt;

should be able to work. However, the tool replies this:

&lt;file&gt;&lt;error&gt;

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
Reddit r/ClaudeAI https://reddit.com/r/RooCode/comments/1n6ip09/read_file_tool_usage_error_seems_to_be_a_bug/
