# OpenClaw 디버깅 루프 토큰 낭비 방지 솔루션

## 1. 문제 분석

### 현상
OpenClaw 에이전트가 디버깅 루프에 빠지면 동일한 실패 접근법을 반복 시도하면서 토큰을 대량 소비한다.
실제 사용량 데이터(`openclaw gateway usage-cost`)에 따르면 하루 $5~17+ 소비가 흔하며,
30일 합산 $526.60 / 454.5M 토큰이 기록되어 있다.

### 근본 원인
1. **Quadratic context growth**: 매 턴마다 전체 대화 히스토리를 재전송 → 비용이 턴 수의 제곱에 비례
2. **No exit condition**: 에이전트가 실패 시 동일한 접근을 무한 반복 (3회 이상 동일 패턴)
3. **No budget awareness**: 작업 시작 전 예상 비용 산정이 없어 초과를 감지할 수 없음
4. **No human-in-the-loop gate**: 토큰 초과 시 자동 일시정지/알림 메커니즘 부재

### OpenClaw 아키텍처 핵심 사항
- **Gateway**: WebSocket 기반, 포트 18789, 로컬 바인드
- **Agent turns**: `openclaw agent -m "task"` 로 에이전트 턴 실행
- **Usage tracking**: `openclaw gateway usage-cost --json` 로 일별 input/output/cache 토큰+비용 조회 가능
- **Telegram channel**: 활성화됨, chat ID `8616468733`, `openclaw message send` 로 메시지 전송
- **Hooks**: `boot-md`, `bootstrap-extra-files`, `command-logger`, `session-memory` 4개 활성
- **Subagents**: 최대 8 동시 실행, `runTimeoutSeconds` 설정 가능
- **Cron**: 스케줄링 지원 (현재 Moltbook 활동 작업 등록됨)
- **Raw stream logging**: `--raw-stream` 옵션으로 모델 스트림 이벤트 jsonl 로깅 가능

## 2. 구현 접근법: Option B - Node.js 모니터링 데몬 + BOOTSTRAP.md 통합

### 선택 이유
- OpenClaw 자체가 Node.js 기반이므로 같은 런타임에서 실행하면 호환성이 좋음
- `openclaw gateway usage-cost --json`으로 실시간 토큰 소비량 조회 가능
- `openclaw message send --channel telegram` 으로 Telegram 알림 직접 전송 가능
- `openclaw agent -m` 으로 에이전트에게 지시를 내릴 수 있음
- BOOTSTRAP.md에 토큰 예산 가이드라인을 주입하여 에이전트 자체에도 자기 제한 장착

## 3. 완전한 구현 코드

### 3.1 토큰 모니터링 데몬 (`token-watchdog.mjs`)

```javascript
#!/usr/bin/env node
// token-watchdog.mjs — OpenClaw 토큰 소비 감시 데몬
// 설치: ~/.openclaw/workspace/token-watchdog.mjs
// 실행: node ~/.openclaw/workspace/token-watchdog.mjs [--estimate <tokens>] [--task <description>]

import { execSync, spawn } from 'child_process';
import { readFileSync, writeFileSync, existsSync, mkdirSync } from 'fs';
import { join } from 'path';

// ── 설정 ──────────────────────────────────────────────────────
const CONFIG = {
  pollIntervalMs: 30_000,          // 30초마다 사용량 체크
  thresholdMultiplier: 2.0,        // 예상 대비 2x 초과 시 경고
  telegramChatId: '8616468733',    // Telegram 알림 대상
  logDir: join(process.env.HOME, '.openclaw', 'workspace', 'memory'),
  logFile: 'token-watchdog.log',
  stateFile: 'token-watchdog-state.json',
  maxRetries: 3,                   // 동일 패턴 최대 재시도
};

// ── 토큰 예상 알고리즘 ─────────────────────────────────────────
const COMPLEXITY_KEYWORDS = {
  high: [
    'debug', 'fix bug', 'refactor', 'migration', 'multi-file',
    'architecture', '디버깅', '리팩토링', '마이그레이션', '버그',
    'complex', 'integration', 'security', 'performance',
  ],
  medium: [
    'implement', 'create', 'build', 'add feature', 'update',
    '구현', '생성', '추가', '업데이트', 'test', 'deploy',
  ],
  low: [
    'read', 'check', 'list', 'status', 'help', 'explain',
    '확인', '조회', '검색', '설명', 'search', 'query',
  ],
};

const TOKEN_ESTIMATES = {
  high:   150_000,   // 디버깅/리팩토링: ~150K 토큰
  medium:  50_000,   // 구현/기능추가: ~50K 토큰
  low:     10_000,   // 조회/확인: ~10K 토큰
  default: 30_000,   // 분류 불가: ~30K 토큰
};

function estimateTokens(taskDescription) {
  if (!taskDescription) return TOKEN_ESTIMATES.default;

  const lower = taskDescription.toLowerCase();
  const descLength = taskDescription.length;

  // 키워드 기반 복잡도 판별
  for (const [level, keywords] of Object.entries(COMPLEXITY_KEYWORDS)) {
    for (const kw of keywords) {
      if (lower.includes(kw)) {
        let estimate = TOKEN_ESTIMATES[level];
        // 설명이 길면 복잡도 상향 조정 (200자 이상이면 1.5x, 500자 이상이면 2x)
        if (descLength > 500) estimate *= 2.0;
        else if (descLength > 200) estimate *= 1.5;
        return Math.round(estimate);
      }
    }
  }

  // 길이 기반 폴백
  if (descLength > 300) return TOKEN_ESTIMATES.medium;
  return TOKEN_ESTIMATES.default;
}

// ── OpenClaw CLI 래퍼 ──────────────────────────────────────────
function getUsageCost(days = 1) {
  try {
    const raw = execSync(
      `openclaw gateway usage-cost --json --days ${days} --timeout 8000`,
      { encoding: 'utf8', timeout: 10_000 }
    );
    return JSON.parse(raw);
  } catch (err) {
    log(`[ERROR] usage-cost 조회 실패: ${err.message}`);
    return null;
  }
}

function sendTelegramAlert(message) {
  try {
    execSync(
      `openclaw message send --channel telegram --to ${CONFIG.telegramChatId} -m ${shellEscape(message)}`,
      { encoding: 'utf8', timeout: 15_000 }
    );
    log(`[ALERT] Telegram 전송 완료`);
  } catch (err) {
    log(`[ERROR] Telegram 전송 실패: ${err.message}`);
  }
}

function pauseAgent() {
  // 에이전트 세션에 일시정지 메시지 전달
  try {
    execSync(
      `openclaw agent -m "⚠️ 토큰 예산 초과로 인해 현재 작업을 일시정지합니다. 지금까지의 진행 상황을 요약하고, 다른 접근 방법을 제안해주세요. 같은 방법을 반복하지 마세요." --timeout 30`,
      { encoding: 'utf8', timeout: 35_000 }
    );
  } catch {
    // 타임아웃은 무시 — 메시지 전달이 목적
  }
}

// ── 유틸리티 ───────────────────────────────────────────────────
function shellEscape(s) {
  return `'${s.replace(/'/g, "'\\''")}'`;
}

function log(msg) {
  const ts = new Date().toISOString();
  const line = `${ts} ${msg}\n`;
  process.stderr.write(line);

  if (!existsSync(CONFIG.logDir)) mkdirSync(CONFIG.logDir, { recursive: true });
  const logPath = join(CONFIG.logDir, CONFIG.logFile);
  try {
    const fd = require('fs').openSync(logPath, 'a');
    require('fs').writeSync(fd, line);
    require('fs').closeSync(fd);
  } catch { /* best-effort */ }
}

function loadState() {
  const statePath = join(CONFIG.logDir, CONFIG.stateFile);
  if (existsSync(statePath)) {
    try {
      return JSON.parse(readFileSync(statePath, 'utf8'));
    } catch { /* fall through */ }
  }
  return { sessions: {}, alertsSent: 0 };
}

function saveState(state) {
  const statePath = join(CONFIG.logDir, CONFIG.stateFile);
  writeFileSync(statePath, JSON.stringify(state, null, 2));
}

// ── 메인 모니터 루프 ──────────────────────────────────────────
async function main() {
  const args = process.argv.slice(2);

  // 인자 파싱
  let manualEstimate = null;
  let taskDescription = null;

  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--estimate' && args[i + 1]) {
      manualEstimate = parseInt(args[i + 1], 10);
      i++;
    } else if (args[i] === '--task' && args[i + 1]) {
      taskDescription = args[i + 1];
      i++;
    } else if (args[i] === '--help') {
      console.log(`
token-watchdog.mjs — OpenClaw 토큰 소비 감시 데몬

사용법:
  node token-watchdog.mjs [옵션]

옵션:
  --task <설명>       작업 설명 (자동 토큰 예상에 사용)
  --estimate <숫자>   수동 토큰 예상치 설정 (자동 예상 무시)
  --help              이 도움말 표시

예시:
  node token-watchdog.mjs --task "Fix login bug in auth module"
  node token-watchdog.mjs --estimate 100000 --task "Complex refactoring"
  node token-watchdog.mjs   # 기본 30,000 토큰 예상
`);
      process.exit(0);
    }
  }

  // 토큰 예상치 결정
  const estimate = manualEstimate || estimateTokens(taskDescription);

  log(`[START] Token Watchdog 시작`);
  log(`[CONFIG] 작업: ${taskDescription || '(미지정)'}`);
  log(`[CONFIG] 예상 토큰: ${estimate.toLocaleString()}`);
  log(`[CONFIG] 경고 임계값: ${(estimate * CONFIG.thresholdMultiplier).toLocaleString()} (${CONFIG.thresholdMultiplier}x)`);
  log(`[CONFIG] 폴링 간격: ${CONFIG.pollIntervalMs / 1000}초`);

  // 시작 시점 사용량 스냅샷
  const baseline = getUsageCost(1);
  if (!baseline) {
    log(`[FATAL] 초기 사용량 조회 실패. Gateway가 실행 중인지 확인하세요.`);
    process.exit(1);
  }

  const todayBaseline = baseline.daily?.[baseline.daily.length - 1];
  const baselineTokens = todayBaseline?.totalTokens || 0;
  const baselineCost = todayBaseline?.totalCost || 0;

  log(`[BASELINE] 시작 시점 토큰: ${baselineTokens.toLocaleString()}, 비용: $${baselineCost.toFixed(4)}`);

  // Telegram 시작 알림
  sendTelegramAlert(
    `🔍 Token Watchdog 시작\n` +
    `📋 작업: ${taskDescription || '미지정'}\n` +
    `📊 예상: ${estimate.toLocaleString()} 토큰\n` +
    `⚠️ 경고 임계값: ${(estimate * CONFIG.thresholdMultiplier).toLocaleString()} 토큰`
  );

  let alertSent = false;
  let consecutiveErrors = 0;

  // 모니터링 루프
  const interval = setInterval(() => {
    const current = getUsageCost(1);
    if (!current) {
      consecutiveErrors++;
      if (consecutiveErrors >= 5) {
        log(`[FATAL] 연속 5회 사용량 조회 실패. 종료합니다.`);
        clearInterval(interval);
        process.exit(1);
      }
      return;
    }
    consecutiveErrors = 0;

    const todayCurrent = current.daily?.[current.daily.length - 1];
    const currentTokens = todayCurrent?.totalTokens || 0;
    const currentCost = todayCurrent?.totalCost || 0;

    const consumed = currentTokens - baselineTokens;
    const costDelta = currentCost - baselineCost;
    const ratio = consumed / estimate;

    log(
      `[MONITOR] 소비: ${consumed.toLocaleString()} / ${estimate.toLocaleString()} 토큰 ` +
      `(${(ratio * 100).toFixed(1)}%) | 비용: $${costDelta.toFixed(4)}`
    );

    // 2x 임계값 초과 체크
    if (ratio >= CONFIG.thresholdMultiplier && !alertSent) {
      alertSent = true;

      const alertMsg =
        `🚨 토큰 예산 초과 경고!\n\n` +
        `예상 ${estimate.toLocaleString()} 토큰이었는데 현재 ${consumed.toLocaleString()} 소비 중.\n` +
        `비율: ${(ratio * 100).toFixed(0)}% (${CONFIG.thresholdMultiplier}x 초과)\n` +
        `비용: $${costDelta.toFixed(4)}\n\n` +
        `계속할까요? (y/n)\n` +
        `→ 응답하려면 Telegram에서 "계속" 또는 "중지" 입력`;

      log(`[ALERT] 2x 임계값 초과! ${consumed.toLocaleString()} > ${(estimate * CONFIG.thresholdMultiplier).toLocaleString()}`);

      // Telegram 경고 전송
      sendTelegramAlert(alertMsg);

      // 에이전트에게 일시정지 요청
      pauseAgent();

      log(`[PAUSED] 에이전트 일시정지 요청 전송됨. 사용자 응답 대기 중...`);

      // 3x 초과 시 자동 종료 경고 설정
      setTimeout(() => {
        const recheck = getUsageCost(1);
        if (recheck) {
          const recheckToday = recheck.daily?.[recheck.daily.length - 1];
          const recheckTokens = (recheckToday?.totalTokens || 0) - baselineTokens;
          if (recheckTokens > estimate * 3) {
            sendTelegramAlert(
              `🛑 토큰 3x 초과! (${recheckTokens.toLocaleString()} 토큰)\n` +
              `자동 감시 종료. 에이전트를 수동으로 중지하세요:\n` +
              `openclaw agent --timeout 1 -m "작업을 즉시 중단하고 진행 상황을 저장하세요."`
            );
            clearInterval(interval);
            process.exit(2);
          }
        }
      }, 120_000); // 2분 후 재확인
    }

    // 작업 완료 감지 (10분간 토큰 변화 없으면)
    // (간단히 구현: 상태 파일에 마지막 토큰 기록)
    const state = loadState();
    const lastTokens = state.lastTokens || 0;
    const lastCheckTime = state.lastCheckTime || Date.now();

    if (consumed === lastTokens && Date.now() - lastCheckTime > 600_000) {
      log(`[DONE] 10분간 토큰 변화 없음. 작업 완료로 판단.`);
      sendTelegramAlert(
        `✅ Token Watchdog 완료\n` +
        `총 소비: ${consumed.toLocaleString()} 토큰 ($${costDelta.toFixed(4)})\n` +
        `예상 대비: ${(ratio * 100).toFixed(0)}%`
      );
      clearInterval(interval);
      process.exit(0);
    }

    state.lastTokens = consumed;
    state.lastCheckTime = consumed !== lastTokens ? Date.now() : lastCheckTime;
    saveState(state);

  }, CONFIG.pollIntervalMs);

  // 시그널 핸들링
  for (const sig of ['SIGINT', 'SIGTERM']) {
    process.on(sig, () => {
      log(`[STOP] ${sig} 수신. 감시 종료.`);
      clearInterval(interval);
      process.exit(0);
    });
  }

  log(`[RUNNING] 모니터링 중... (Ctrl+C로 종료)`);
}

main().catch(err => {
  log(`[FATAL] ${err.message}`);
  process.exit(1);
});
```

### 3.2 BOOTSTRAP.md 토큰 예산 가이드라인 주입

에이전트의 시스템 프롬프트에 토큰 자기 제한 규칙을 추가한다. 이 내용을 `~/.openclaw/workspace/TOKEN-BUDGET.md` 파일로 저장하고 `bootstrap-extra-files` 훅으로 주입한다.

```markdown
# TOKEN-BUDGET.md - 토큰 예산 자기 제한 규칙

## 핵심 규칙: 디버깅 루프 탈출

당신은 토큰을 소비하는 AI 에이전트입니다. 불필요한 토큰 낭비를 방지하기 위해 다음 규칙을 반드시 따르세요.

### 1. 3회 반복 규칙
동일한 에러나 실패를 3번 이상 만나면:
- **즉시 멈추세요**
- 지금까지 시도한 접근법을 요약하세요
- 완전히 다른 접근법을 제안하세요
- 사용자에게 확인을 요청하세요

### 2. 단계별 확인
복잡한 작업은 단계를 나누어 각 단계 완료 후 진행 여부를 확인하세요:
- "1단계 완료. 다음 단계로 진행할까요?"
- 중간 결과를 메모리에 저장하세요 (작업 손실 방지)

### 3. 비용 인식
- 단순 조회/확인: ~10,000 토큰 이내
- 기능 구현/수정: ~50,000 토큰 이내
- 디버깅/리팩토링: ~150,000 토큰 이내
- 이 범위를 크게 초과한다면 접근법을 재고하세요

### 4. 컨텍스트 절약
- 이전 턴의 전체 출력을 반복하지 마세요
- 도구 호출 결과는 핵심만 보존하세요
- 불필요한 파일 전체 읽기를 피하세요 (필요한 부분만 읽기)

### 5. 에스컬레이션
해결할 수 없다고 판단되면 솔직하게 말하세요:
"이 문제는 제가 해결하기 어렵습니다. [이유]. [대안 제안]."
```

### 3.3 bootstrap-extra-files 훅 설정

OpenClaw 설정에 `TOKEN-BUDGET.md`를 자동 주입하도록 설정:

```bash
# bootstrap-extra-files 훅이 TOKEN-BUDGET.md를 주입하도록 설정
openclaw config set hooks.internal.entries.bootstrap-extra-files.options.patterns '["TOKEN-BUDGET.md"]'
```

### 3.4 Cron 기반 주기적 비용 점검 (`token-daily-report.sh`)

```bash
#!/bin/bash
# token-daily-report.sh — 일일 토큰 사용 리포트를 Telegram으로 전송
# 설치: ~/.openclaw/workspace/token-daily-report.sh
# 크론: openclaw cron 에 등록

set -euo pipefail

REPORT=$(openclaw gateway usage-cost --json --days 7 2>/dev/null)

if [ -z "$REPORT" ]; then
  openclaw message send --channel telegram --to 8616468733 \
    -m '❌ 일일 토큰 리포트 생성 실패: Gateway 연결 불가'
  exit 1
fi

# jq로 파싱 (jq 없으면 node 사용)
if command -v jq &>/dev/null; then
  TODAY_COST=$(echo "$REPORT" | jq -r '.daily[-1].totalCost // 0')
  TODAY_TOKENS=$(echo "$REPORT" | jq -r '.daily[-1].totalTokens // 0')
  WEEK_COST=$(echo "$REPORT" | jq -r '.totals.totalCost // 0')
  WEEK_TOKENS=$(echo "$REPORT" | jq -r '.totals.totalTokens // 0')
  TODAY_DATE=$(echo "$REPORT" | jq -r '.daily[-1].date // "unknown"')
else
  eval $(node -e "
    const d = $REPORT;
    const t = d.daily[d.daily.length-1];
    console.log('TODAY_COST=' + (t?.totalCost||0).toFixed(4));
    console.log('TODAY_TOKENS=' + (t?.totalTokens||0));
    console.log('WEEK_COST=' + (d.totals?.totalCost||0).toFixed(4));
    console.log('WEEK_TOKENS=' + (d.totals?.totalTokens||0));
    console.log('TODAY_DATE=' + (t?.date||'unknown'));
  ")
fi

MSG="📊 일일 토큰 리포트 ($TODAY_DATE)

오늘: ${TODAY_TOKENS} 토큰 / \$${TODAY_COST}
7일 합계: ${WEEK_TOKENS} 토큰 / \$${WEEK_COST}

디버깅 루프 경고 임계값: 150,000 토큰/작업"

openclaw message send --channel telegram --to 8616468733 -m "$MSG"
```

## 4. 설치 및 사용 방법

### 4.1 파일 설치

```bash
# 1. 모니터링 데몬 설치
cp token-watchdog.mjs ~/.openclaw/workspace/token-watchdog.mjs
chmod +x ~/.openclaw/workspace/token-watchdog.mjs

# 2. 토큰 예산 가이드라인 설치
cp TOKEN-BUDGET.md ~/.openclaw/workspace/TOKEN-BUDGET.md

# 3. 일일 리포트 스크립트 설치
cp token-daily-report.sh ~/.openclaw/workspace/token-daily-report.sh
chmod +x ~/.openclaw/workspace/token-daily-report.sh

# 4. bootstrap-extra-files에 TOKEN-BUDGET.md 등록
openclaw config set hooks.internal.entries.bootstrap-extra-files.options.patterns '["TOKEN-BUDGET.md"]'
```

### 4.2 사용법

#### 작업 시작 전 감시 데몬 실행

```bash
# 기본 사용 (자동 토큰 예상)
node ~/.openclaw/workspace/token-watchdog.mjs --task "Fix login bug in auth module"

# 수동 토큰 예상치 지정
node ~/.openclaw/workspace/token-watchdog.mjs --estimate 100000 --task "Complex DB migration"

# 백그라운드 실행
nohup node ~/.openclaw/workspace/token-watchdog.mjs --task "Deploy hotfix" &
```

#### 별도 터미널에서 에이전트 작업 실행

```bash
# 에이전트에게 작업 지시
openclaw agent -m "Fix the authentication timeout bug in the login module"
```

#### 일일 리포트 크론 등록

```bash
# OpenClaw 크론으로 매일 오전 9시 리포트
openclaw cron create \
  --name "일일 토큰 리포트" \
  --schedule "0 9 * * *" \
  --tz "Asia/Seoul" \
  --message "다음 스크립트를 실행하세요: bash ~/.openclaw/workspace/token-daily-report.sh"
```

### 4.3 tmux 통합 (추천)

```bash
# tmux 세션에서 감시 데몬을 별도 패인으로 실행
tmux split-window -h "node ~/.openclaw/workspace/token-watchdog.mjs --task 'Current task description'"
```

## 5. Telegram 알림 동작 방식

### 알림 흐름

```
작업 시작
  │
  ├─→ Telegram: "🔍 Token Watchdog 시작 / 예상: 50,000 토큰"
  │
  ▼
30초마다 토큰 사용량 폴링
(openclaw gateway usage-cost --json)
  │
  ├─ 소비 < 2x 예상 → 계속 모니터링
  │
  ├─ 소비 >= 2x 예상 → 알림 트리거
  │   │
  │   ├─→ Telegram: "🚨 예상 50,000 토큰이었는데 현재 105,000 소비 중. 계속할까요?"
  │   │
  │   └─→ 에이전트에게 일시정지 메시지 전송
  │
  ├─ 소비 >= 3x 예상 (2분 후 재확인)
  │   │
  │   └─→ Telegram: "🛑 토큰 3x 초과! 자동 감시 종료."
  │
  └─ 10분간 변화 없음
      │
      └─→ Telegram: "✅ Token Watchdog 완료 / 총 소비: 45,000 토큰"
```

### Telegram 메시지 전송 메커니즘

OpenClaw의 내장 `message send` 명령어를 사용:

```bash
openclaw message send \
  --channel telegram \
  --to 8616468733 \
  -m "알림 메시지"
```

이 방식은:
- 별도의 Telegram Bot API 호출 코드가 필요 없음
- OpenClaw이 이미 설정된 봇 토큰(`8588348752:AAHz...`)을 사용
- 기존 Telegram 채널 설정을 그대로 활용
- `--buttons` 옵션으로 인라인 키보드(y/n 버튼)도 추가 가능

### 사용자 응답 처리

현재 구현에서는 Telegram 알림 후 에이전트에게 직접 일시정지 메시지를 전달한다. 사용자가 Telegram에서 "계속" 또는 "중지"를 입력하면 OpenClaw 에이전트가 해당 메시지를 수신하여 작업을 재개하거나 중단한다.

향후 개선으로 `--buttons` 옵션을 활용한 인라인 키보드 응답도 가능:

```bash
openclaw message send \
  --channel telegram \
  --to 8616468733 \
  -m "토큰 예산 초과. 계속할까요?" \
  --buttons '[
    [{"text": "✅ 계속", "callback_data": "continue"}],
    [{"text": "🛑 중지", "callback_data": "stop"}]
  ]'
```

## 6. 아키텍처 다이어그램

```
┌─────────────────────────────────────────────────────────┐
│                    사용자 (Telegram)                       │
│  ┌─────────────────────────────────────────────────┐    │
│  │ "🚨 예상 50K인데 현재 105K 소비 중. 계속? (y/n)"  │    │
│  └─────────────────────────────────────────────────┘    │
└───────────────────────┬─────────────────────────────────┘
                        │ openclaw message send
                        │
┌───────────────────────┴─────────────────────────────────┐
│              token-watchdog.mjs (데몬)                    │
│                                                          │
│  ┌──────────┐   ┌──────────────┐   ┌──────────────┐    │
│  │ 토큰 예상 │   │ 30초 폴링     │   │ 임계값 체크   │    │
│  │ 알고리즘  │   │ usage-cost   │   │ 2x / 3x     │    │
│  └──────────┘   └──────┬───────┘   └──────┬───────┘    │
│                         │                   │            │
└─────────────────────────┼───────────────────┼────────────┘
                          │                   │
                ┌─────────┴─────────┐         │
                │  OpenClaw Gateway  │         │
                │  (port 18789)     │         │
                │                   │         │
                │  usage-cost API   │         │
                │  agent turns      │◄────────┘
                │  session logs     │  pauseAgent()
                └───────────────────┘
                          │
                ┌─────────┴─────────┐
                │  Claude API       │
                │  (Anthropic)      │
                │                   │
                │  input_tokens     │
                │  output_tokens    │
                │  cache_read/write │
                └───────────────────┘
```

```
┌───────────────────────────────────────────────────┐
│            에이전트 자기 제한 (BOOTSTRAP)             │
│                                                    │
│  TOKEN-BUDGET.md (bootstrap-extra-files 훅으로 주입) │
│  ┌──────────────────────────────────────────┐     │
│  │ • 3회 반복 규칙 → 다른 접근법 시도          │     │
│  │ • 단계별 확인 → 중간 결과 저장             │     │
│  │ • 비용 인식 → 범위 초과 시 재고            │     │
│  │ • 컨텍스트 절약 → 필요한 부분만 읽기       │     │
│  │ • 에스컬레이션 → 솔직하게 한계 인정        │     │
│  └──────────────────────────────────────────┘     │
└───────────────────────────────────────────────────┘
```

## 7. 비용 절감 예상

| 시나리오 | Watchdog 없이 | Watchdog 있을 때 | 절감 |
|---------|-------------|----------------|------|
| 단순 버그 수정 | 50K~200K 토큰 | 10K~50K 토큰 | 60~75% |
| 디버깅 루프 진입 | 200K~500K 토큰 | 100K~150K (2x에서 중단) | 50~70% |
| 복합 리팩토링 | 300K~1M 토큰 | 150K~300K 토큰 | 50~70% |
| 월간 추정 (현재 $526/월 기준) | $526 | $200~260 | ~50% ($260+/월) |

## 8. 제한사항 및 향후 개선

### 현재 제한
- `usage-cost`는 세션별이 아닌 일별 합산 → 동시 다중 작업 시 개별 추적 불가
- Telegram 응답 수신은 에이전트 세션을 통해 간접적으로 처리
- 토큰 예상 알고리즘은 키워드 기반으로 단순함

### 향후 개선 방향
1. **세션 ID별 토큰 추적**: `--raw-stream` 로그를 파싱하여 세션별 사용량 분리
2. **학습 기반 예상**: 과거 작업 이력으로 예상 정확도 향상 (memory 디렉토리 활용)
3. **OpenClaw 플러그인화**: `openclaw plugins install` 로 설치 가능한 공식 플러그인으로 패키징
4. **실시간 WebSocket 모니터링**: Gateway WebSocket에 직접 연결하여 턴별 토큰 추적
5. **SynapseAI 연동**: 디버깅 루프 진입 시 SynapseAI 솔루션 DB 자동 검색
