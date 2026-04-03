#!/usr/bin/env node
/**
 * MoltCoin Distribution Script
 *
 * 주당 총 배포량은 고정 (4,326,923 MOLT, 2년 반감기)
 * 배포 빈도는 페이즈에 따라 조정됨:
 *   early  — 3시간마다  1회 → 1회당 77,266 MOLT
 *   mid    — 12시간마다 1회 → 1회당 308,923 MOLT
 *   weekly — 주 1회        → 1회당 4,326,923 MOLT
 *
 * Usage:
 *   node distribute.js --recipients agent1,agent2,agent3
 *   node distribute.js --recipients-file recipients.txt
 *   node distribute.js --phase early --recipients agent1,agent2
 *   node distribute.js --dry-run --recipients agent1
 */

import { readFileSync, writeFileSync } from 'fs';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';
import { createHash } from 'crypto';

const __dir = dirname(fileURLToPath(import.meta.url));
const LEDGER_PATH       = join(__dir, 'ledger.json');
const TRANSACTIONS_PATH = join(__dir, 'transactions.json');

// ── 상수 ─────────────────────────────────────────────────────
const WEEK_1_EMISSION  = 4_326_923;  // 900,000,000 / 208
const HALVING_WEEKS    = 104;         // 2년 = 104주
const TOTAL_SUPPLY     = 1_000_000_000;

const PHASES = {
  early:  { interval_hours: 3   },
  mid:    { interval_hours: 12  },
  weekly: { interval_hours: 168 },
};

// ── 주차별 주간 발행량 ────────────────────────────────────────
function weeklyEmission(week) {
  const epoch = Math.floor((week - 1) / HALVING_WEEKS);
  return Math.floor(WEEK_1_EMISSION / Math.pow(2, epoch));
}

// ── 1회 배포량 계산 ───────────────────────────────────────────
// 1회당량 = 주간총량 / (168 / interval_hours)
function perRoundEmission(weeklyTotal, intervalHours) {
  const roundsPerWeek = 168 / intervalHours;
  return Math.floor(weeklyTotal / roundsPerWeek);
}

// ── 인자 파싱 ─────────────────────────────────────────────────
function parseArgs() {
  const args = process.argv.slice(2);
  let recipients = [];
  let dryRun = false;
  let phaseOverride = null;
  let forceWeek = null;

  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--recipients' && args[i + 1]) {
      recipients = args[++i].split(',').map(r => r.trim()).filter(Boolean);
    } else if (args[i] === '--recipients-file' && args[i + 1]) {
      recipients = readFileSync(args[++i], 'utf8')
        .split('\n').map(r => r.trim()).filter(Boolean);
    } else if (args[i] === '--phase' && args[i + 1]) {
      phaseOverride = args[++i];
    } else if (args[i] === '--week' && args[i + 1]) {
      forceWeek = parseInt(args[++i], 10);
    } else if (args[i] === '--dry-run') {
      dryRun = true;
    } else if (args[i] === '--help') {
      console.log(`
MoltCoin Distribution Script

Usage:
  node distribute.js --recipients agent1,agent2
  node distribute.js --recipients-file recipients.txt
  node distribute.js --phase early|mid|weekly --recipients agent1
  node distribute.js --dry-run --recipients agent1

Phase intervals:
  early   — every 3h   → ${perRoundEmission(WEEK_1_EMISSION, 3).toLocaleString()} MOLT/round
  mid     — every 12h  → ${perRoundEmission(WEEK_1_EMISSION, 12).toLocaleString()} MOLT/round
  weekly  — every 168h → ${WEEK_1_EMISSION.toLocaleString()} MOLT/round
`);
      process.exit(0);
    }
  }

  if (recipients.length === 0) {
    console.error('Error: --recipients or --recipients-file required');
    process.exit(1);
  }
  if (phaseOverride && !PHASES[phaseOverride]) {
    console.error(`Error: unknown phase "${phaseOverride}". Use: early, mid, weekly`);
    process.exit(1);
  }

  return { recipients, dryRun, phaseOverride, forceWeek };
}

// ── 메인 ─────────────────────────────────────────────────────
function main() {
  const { recipients, dryRun, phaseOverride, forceWeek } = parseArgs();

  const ledger = JSON.parse(readFileSync(LEDGER_PATH, 'utf8'));
  const transactions = JSON.parse(readFileSync(TRANSACTIONS_PATH, 'utf8'));

  // 페이즈 결정
  const phase = phaseOverride ?? ledger.current_phase ?? 'weekly';
  const intervalHours = PHASES[phase].interval_hours;

  // 현재 주차 (첫 배포부터 경과 시간으로 계산)
  const currentWeek = forceWeek ?? Math.max(1, ledger.week || 1);
  const weeklyTotal = weeklyEmission(currentWeek);
  const roundEmission = perRoundEmission(weeklyTotal, intervalHours);
  const perAgent = Math.floor(roundEmission / recipients.length);
  const remainder = roundEmission - (perAgent * recipients.length);
  const now = new Date().toISOString();

  // 하드캡 검증
  if (ledger.distribution_remaining < roundEmission) {
    console.error('Error: Distribution pool exhausted.');
    process.exit(1);
  }
  if (ledger.circulating + roundEmission > TOTAL_SUPPLY) {
    console.error('Error: HARDCAP VIOLATION — would exceed 1,000,000,000 MOLT');
    process.exit(1);
  }

  // 미리보기
  const roundsPerWeek = 168 / intervalHours;
  console.log(`\n=== MoltCoin Distribution ===`);
  console.log(`Phase:            ${phase} (${intervalHours}h interval, ${roundsPerWeek}x/week)`);
  console.log(`Week:             ${currentWeek}`);
  console.log(`Weekly total:     ${weeklyTotal.toLocaleString()} MOLT`);
  console.log(`This round:       ${roundEmission.toLocaleString()} MOLT`);
  console.log(`Recipients:       ${recipients.length} agents`);
  console.log(`Per agent:        ${perAgent.toLocaleString()} MOLT`);
  console.log(`Remainder:        ${remainder} MOLT → FOUNDER:master`);
  console.log(`Pool remaining:   ${(ledger.distribution_remaining - roundEmission).toLocaleString()}`);
  console.log(`Circulating:      ${(ledger.circulating + roundEmission).toLocaleString()}`);

  if (dryRun) {
    console.log('\n[DRY RUN] No changes saved.');
    return;
  }

  // 잔액 업데이트
  for (const agent of recipients) {
    ledger.balances[agent] = (ledger.balances[agent] || 0) + perAgent;
  }
  if (remainder > 0) {
    ledger.balances['FOUNDER:master'] = (ledger.balances['FOUNDER:master'] || 0) + remainder;
  }

  // 트랜잭션 기록
  for (const agent of recipients) {
    transactions.push({
      week: currentWeek,
      date: now,
      type: 'distribution',
      phase,
      interval_hours: intervalHours,
      recipient: agent,
      amount: perAgent,
      reason: `Week ${currentWeek} ${phase} distribution`,
      tx_hash: createHash('sha256')
        .update(`${currentWeek}|${phase}|${agent}|${perAgent}|${now}`)
        .digest('hex'),
    });
  }
  if (remainder > 0) {
    transactions.push({
      week: currentWeek,
      date: now,
      type: 'distribution_remainder',
      phase,
      recipient: 'FOUNDER:master',
      amount: remainder,
      reason: `Week ${currentWeek} ${phase} remainder`,
      tx_hash: createHash('sha256')
        .update(`${currentWeek}|${phase}|FOUNDER:master|${remainder}|${now}`)
        .digest('hex'),
    });
  }

  // 장부 업데이트
  const nextDate = new Date();
  nextDate.setHours(nextDate.getHours() + intervalHours);

  ledger.week = currentWeek;
  ledger.current_phase = phase;
  ledger.interval_hours = intervalHours;
  ledger.circulating += roundEmission;
  ledger.distribution_remaining -= roundEmission;
  ledger.last_updated = now;
  ledger.next_distribution = nextDate.toISOString();

  writeFileSync(LEDGER_PATH, JSON.stringify(ledger, null, 2));
  writeFileSync(TRANSACTIONS_PATH, JSON.stringify(transactions, null, 2));

  console.log(`\n✅ Distribution complete.`);
  console.log(`   Next: ${nextDate.toISOString()} (${intervalHours}h later)`);
  console.log(`   Commit moltcoin/ledger.json + transactions.json to publish.`);
}

main();
