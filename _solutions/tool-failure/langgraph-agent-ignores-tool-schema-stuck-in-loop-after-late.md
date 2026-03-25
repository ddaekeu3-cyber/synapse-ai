---
layout: solution
title: "LangGraph agent ignores tool schema / stuck in loop after latest update – anyone else?"
category: tool-failure
source: Reddit r/ClaudeAI https://reddit.com/r/LangChain/comments/1rjv6q7/langgraph_agen
---

# LangGraph agent ignores tool schema / stuck in loop after latest update – anyone else?

## 증상
Hey everyone,

I'm building a multi-step research agent with LangGraph (v0.3.x) + Claude 3.5 Sonnet / GPT-4o-mini.
The node looks roughly like:


research_agent = create_react_agent(
    model=ChatOpenAI(model="gpt-4o-mini"),
    tools=[wikipedia_tool, tavily_search, arxiv_tool],
    prompt=research_prompt,
    checkpointer=MemorySaver()
)

But after 2–3 steps it starts ignoring the tool schema an

## 원인
보고된 버그/문제. 카테고리: tool-failure.

## 해결법
1. 에러 메시지 정확히 읽기: 에러 코드로 원인 파악
2. 권한 확인: API 키, 토큰, 스코프 확인
3. 버전 호환성: 도구/API 버전 확인
4. 대체 도구: 실패 시 동일 기능의 대체 도구 사용
5. 재시도: 일시적 오류는 지수 백오프로 재시도

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
Reddit r/ClaudeAI https://reddit.com/r/LangChain/comments/1rjv6q7/langgraph_agent_ignores_tool_schema_stuck_in_loop/
