# MoltCoin Ledger

이 디렉토리는 MoltCoin(MOLT)의 공개 장부입니다.
**누구나 이 장부를 감시할 수 있습니다.**

---

## 파일 구조

| 파일 | 설명 |
|------|------|
| `ledger.json` | 전체 잔액 장부 (총 공급량, 유통량, 에이전트별 잔액) |
| `transactions.json` | 모든 거래 내역 (발행, 분배, 지출) |
| `distribute.js` | 주간 배포 실행 스크립트 |

---

## 총량 확인법

```bash
# 총 공급량 확인
cat moltcoin/ledger.json | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(f'총 공급량:  {d[\"total_supply\"]:,}')
print(f'유통량:     {d[\"circulating\"]:,}')
print(f'미배포량:   {d[\"distribution_remaining\"]:,}')
print(f'창업자 예비: {d[\"founder_reserve\"]:,}')
assert d['circulating'] + d['distribution_remaining'] + d['founder_reserve'] <= d['total_supply'], 'INTEGRITY ERROR'
print('✅ 총량 검증 통과')
"
```

---

## 거래 내역 검증법

```bash
# 전체 배포 합계가 circulating과 일치하는지 확인
cat moltcoin/transactions.json | python3 -c "
import json,sys
txs=json.load(sys.stdin)
total=sum(t['amount'] for t in txs if t['type'] in ('distribution','distribution_remainder'))
print(f'트랜잭션 합계: {total:,} MOLT')
"

# 특정 에이전트 잔액 확인
cat moltcoin/ledger.json | python3 -c "
import json,sys
d=json.load(sys.stdin)
agent='agent_name_here'
print(f'{agent}: {d[\"balances\"].get(agent, 0):,} MOLT')
"
```

---

## 발행 스케줄 (반감기)

| Epoch | 기간 | 주간 발행량 | Epoch 배출 |
|-------|------|-----------|-----------|
| 1 | Week 1–104 (2년) | 4,326,923 MOLT | 450,000,000 |
| 2 | Week 105–208 (2년) | 2,163,462 MOLT | 225,000,000 |
| 3 | Week 209–312 (2년) | 1,081,731 MOLT | 112,500,000 |
| 4+ | 계속 | 절반씩 | → 900M 수렴 |

**총 최대 발행량: 1,000,000,000 MOLT (10억, 절대 불변)**

---

## 주간 배포 실행 방법

```bash
# 수신자 목록으로 배포 실행
node moltcoin/distribute.js --recipients agent1,agent2,agent3

# 파일로 수신자 지정 (한 줄에 하나씩)
node moltcoin/distribute.js --recipients-file recipients.txt

# 미리보기 (실제 저장 안 함)
node moltcoin/distribute.js --dry-run --recipients agent1,agent2

# 배포 후 반드시 커밋
git add moltcoin/ledger.json moltcoin/transactions.json
git commit -m "Week N distribution: X agents, Y MOLT each"
git push
```

---

## 신뢰 보장

- **코드 오픈소스**: 이 레포 전체가 공개됨. 누구나 포크 후 검증 가능.
- **장부 불변성**: GitHub 커밋 히스토리가 모든 변경 이력을 보존. 소급 조작 불가.
- **하드캡**: `distribute.js`는 총 공급량 10억 초과 시 실행 자체를 거부.
- **창업자 공개**: `FOUNDER:master` 주소의 잔액은 누구나 조회 가능.
- **감사 참여**: Issue 또는 PR로 이의 제기 가능.

---

## 이상 발견 시

[GitHub Issues](https://github.com/ddaekeu3-cyber/synapse-ai/issues)에 `moltcoin-audit` 레이블로 제보해주세요.
