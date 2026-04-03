#!/usr/bin/env node
/**
 * MoltCoin Weekly Distribution Script
 *
 * Usage:
 *   node distribute.js --recipients alice,bob,charlie
 *   node distribute.js --recipients-file recipients.txt
 *   node distribute.js --dry-run --recipients alice,bob
 *
 * recipients.txt: one agent name per line
 *
 * Halving schedule:
 *   Week 1 emission: 4,326,923 MOLT
 *   Halving every 104 weeks (2 years)
 *   Total converges to ~900,000,000 MOLT
 */

import { readFileSync, writeFileSync } from 'fs';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';
import { createHash } from 'crypto';

const __dir = dirname(fileURLToPath(import.meta.url));

const LEDGER_PATH      = join(__dir, 'ledger.json');
const TRANSACTIONS_PATH = join(__dir, 'transactions.json');

// ── 상수 ─────────────────────────────────────────────────────
const WEEK_1_EMISSION  = 4_326_923;   // 900,000,000 / 208
const HALVING_WEEKS    = 104;          // 2년 = 104주
const TOTAL_SUPPLY     = 1_000_000_000;
const FOUNDER_RESERVE  = 100_000_000;
const DISTRIBUTION_CAP = 900_000_000;

// ── 주차별 발행량 계산 ────────────────────────────────────────
function weeklyEmission(week) {
  if (week < 1) throw new Error('Week must be >= 1');
  const epoch = Math.floor((week - 1) / HALVING_WEEKS);
  const emission = Math.floor(WEEK_1_EMISSION / Math.pow(2, epoch));
  return emission;
}

// ── 인자 파싱 ─────────────────────────────────────────────────
function parseArgs() {
  const args = process.argv.slice(2);
  let recipients = [];
  let dryRun = false;
  let forceWeek = null;

  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--recipients' && args[i + 1]) {
      recipients = args[++i].split(',').map(r => r.trim()).filter(Boolean);
    } else if (args[i] === '--recipients-file' && args[i + 1]) {
      const file = args[++i];
      recipients = readFileSync(file, 'utf8')
        .split('\n').map(r => r.trim()).filter(Boolean);
    } else if (args[i] === '--dry-run') {
      dryRun = true;
    } else if (args[i] === '--week' && args[i + 1]) {
      forceWeek = parseInt(args[++i], 10);
    } else if (args[i] === '--help') {
      console.log(`
MoltCoin Weekly Distribution

Usage:
  node distribute.js --recipients agent1,agent2,agent3
  node distribute.js --recipients-file recipients.txt
  node distribute.js --dry-run --recipients agent1,agent2
  node distribute.js --week 3 --recipients agent1   (force specific week)

Options:
  --recipients <list>       Comma-separated agent names
  --recipients-file <path>  One agent per line text file
  --dry-run                 Preview without saving
  --week <n>                Override week number (for testing)
  --help                    Show this help
`);
      process.exit(0);
    }
  }

  if (recipients.length === 0) {
    console.error('Error: No recipients specified. Use --recipients or --recipients-file');
    process.exit(1);
  }

  return { recipients, dryRun, forceWeek };
}

// ── 메인 ─────────────────────────────────────────────────────
function main() {
  const { recipients, dryRun, forceWeek } = parseArgs();

  // 장부 읽기
  const ledger = JSON.parse(readFileSync(LEDGER_PATH, 'utf8'));
  const transactions = JSON.parse(readFileSync(TRANSACTIONS_PATH, 'utf8'));

  const currentWeek = forceWeek ?? (ledger.week + 1);
  const emission    = weeklyEmission(currentWeek);
  const perAgent    = Math.floor(emission / recipients.length);
  const remainder   = emission - (perAgent * recipients.length);
  const now         = new Date().toISOString();

  // 검증
  if (ledger.distribution_remaining < emission) {
    console.error(`Error: Distribution pool exhausted.`);
    console.error(`  Remaining: ${ledger.distribution_remaining.toLocaleString()}`);
    console.error(`  Needed:    ${emission.toLocaleString()}`);
    process.exit(1);
  }
  if (ledger.circulating + emission > TOTAL_SUPPLY) {
    console.error('Error: HARDCAP VIOLATION — would exceed 1,000,000,000 MOLT');
    process.exit(1);
  }

  // 배포 미리보기
  console.log(`\n=== MoltCoin Week ${currentWeek} Distribution ===`);
  console.log(`Emission:         ${emission.toLocaleString()} MOLT`);
  console.log(`Recipients:       ${recipients.length} agents`);
  console.log(`Per agent:        ${perAgent.toLocaleString()} MOLT`);
  console.log(`Remainder:        ${remainder} MOLT (→ FOUNDER:master)`);
  console.log(`Distribution pool remaining after: ${(ledger.distribution_remaining - emission).toLocaleString()}`);
  console.log(`Circulating after: ${(ledger.circulating + emission).toLocaleString()}`);
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
  const txBase = {
    week: currentWeek,
    date: now,
    type: 'weekly_distribution',
    emission_total: emission,
    per_agent: perAgent,
    recipients: recipients.length,
    tx_hash: createHash('sha256')
      .update(`${currentWeek}|${now}|${emission}|${recipients.join(',')}`)
      .digest('hex')
  };

  // 수신자별 개별 트랜잭션
  for (const agent of recipients) {
    transactions.push({
      week: currentWeek,
      date: now,
      type: 'distribution',
      recipient: agent,
      amount: perAgent,
      reason: `Week ${currentWeek} distribution`,
      tx_hash: createHash('sha256')
        .update(`${currentWeek}|${agent}|${perAgent}|${now}`)
        .digest('hex')
    });
  }
  if (remainder > 0) {
    transactions.push({
      week: currentWeek,
      date: now,
      type: 'distribution_remainder',
      recipient: 'FOUNDER:master',
      amount: remainder,
      reason: `Week ${currentWeek} remainder`,
      tx_hash: createHash('sha256')
        .update(`${currentWeek}|FOUNDER:master|${remainder}|${now}`)
        .digest('hex')
    });
  }

  // 장부 업데이트
  ledger.week = currentWeek;
  ledger.circulating += emission;
  ledger.distribution_remaining -= emission;
  ledger.last_updated = now;

  // 다음 배포일 (7일 후)
  const nextDate = new Date();
  nextDate.setDate(nextDate.getDate() + 7);
  ledger.next_distribution = nextDate.toISOString();

  // 저장
  writeFileSync(LEDGER_PATH, JSON.stringify(ledger, null, 2));
  writeFileSync(TRANSACTIONS_PATH, JSON.stringify(transactions, null, 2));

  console.log(`\n✅ Week ${currentWeek} distribution complete.`);
  console.log(`   Ledger saved:       moltcoin/ledger.json`);
  console.log(`   Transactions saved: moltcoin/transactions.json`);
  console.log(`   Commit these files to publish the distribution.`);
}

main();
