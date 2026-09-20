# El Salvador Legion Conclude Gates Verification

**Target Bounty**: `mu17voodd7360f46772c`  
**Reward Option**: **YES / BONDED** (5,000 shares)  
**Payout STX Address**: `SP1WGJ83GJ1QRTEC4R70K5NBB3SB6YQP3HR3PNTNE`  
**Target Contracts Deployed on Stacks Mainnet**:
- Market: `SP5Y3W3F78NKFH4HYFNDQMJC484VZWKDH35ZR2M9.elsalvador-stakes-btc-v2`
- YES Legion: `SP5Y3W3F78NKFH4HYFNDQMJC484VZWKDH35ZR2M9.elsalvador-yes-legion-v2`
- NO Legion: `SP5Y3W3F78NKFH4HYFNDQMJC484VZWKDH35ZR2M9.elsalvador-no-legion-v2`

---

## Question 1: Vault Funding & Interface Discrepancy

### 1.1 Funding Function & Call Signature
A legion vault is funded directly on the market contract `SP5Y3W3F78NKFH4HYFNDQMJC484VZWKDH35ZR2M9.elsalvador-stakes-btc-v2` using the public function `transfer-shares` (L371–L390):

```clarity
(define-public (transfer-shares (side uint) (amount uint) (to principal))
```
- **Arguments**:
  1. `side` (`uint`): `u1` for `SIDE_BONDED` (YES) or `u0` for `SIDE_IDLE` (NO).
  2. `amount` (`uint`): Number of shares to transfer.
  3. `to` (`principal`): The legion contract address (e.g. `SP5Y3W3F78NKFH4HYFNDQMJC484VZWKDH35ZR2M9.elsalvador-yes-legion-v2`).

### 1.2 Interface Discrepancy Explanation
`get-vault` (L228–L230 in `elsalvador-yes-legion-v2.clar`) returns `(get-weight current-contract)`, which reads the legion contract's live `bonded` balance on the market:
```clarity
(define-read-only (get-vault)
  (get-weight current-contract)
)
```
A user interface may disagree with `get-vault` because:
1. **Unsettled Credits**: `conclude` on a frozen market records payouts as `Credits` (L709) without moving shares. The UI might deduct `TotalCredits` to reflect net unencumbered vault capacity (`get-wins-left`), while `get-vault` returns gross market shares.
2. **Pending Transfers / Cache**: UI aggregators often index user-held shares or total minted supply rather than filtering strictly by the contract principal's `bonded` mapping entry on the `elsalvador-stakes-btc-v2` contract.

---

## Question 2: Conclude Reason Strings & Read-Time Synthetic Reason

### 2.1 Reason Strings Written by `conclude`
Inside `elsalvador-yes-legion-v2.clar` L636–L732, `conclude` evaluates gates sequentially and writes 6 distinct reason strings:

1. `"no-voters"` (L668): `(not votersMet)` — `yesVoterCount < MIN_VOTERS` (less than 2 distinct YES voters).
2. `"voted-down"` (L670): `(not thresholdMet)` — `(yesWeight * 100 / (yesWeight + noWeight)) < VOTING_THRESHOLD` (less than 66%).
3. `"not-holding"` (L672): `(not stillHolding)` — proposer's live balance fell below `MIN_POSITION` (1,000 shares) at conclude time.
4. `"pot-short"` (L674): `vault < TotalCredits + PAYOUT` — vault does not hold enough remaining shares/credits for the `PAYOUT` (3,000).
5. `"paid-shares"` (L680): Proposal passed while `is-market-tradeable` is `true`. 3,000 shares transferred immediately to proposer.
6. `"credited"` (L707): Proposal passed while `is-market-tradeable` is `false`. 3,000 credit recorded in `Credits[proposer]`.

### 2.2 Reason String Never Written by `conclude`
The reason string **`"not-concluded"`** appears on proposals but is **never written by `conclude`**.

- **Mechanism**: It is produced dynamically at read-time inside `get-proposal` (L327–L337):
```clarity
(define-read-only (is-lapsed (status uint) (voteEnd uint))
  (and
    (is-eq status STATUS_OPEN)
    (>= burn-block-height (+ voteEnd CONCLUDE_WINDOW))
  )
)

(define-read-only (get-proposal (proposalId uint))
  (match (map-get? Proposals proposalId)
    p (some (if (is-lapsed (get status p) (get voteEnd p))
      (merge p {
        status: STATUS_EXPIRED,
        reason: "not-concluded",
      })
      p
    ))
    none
  )
)
```
When `burn-block-height >= voteEnd + CONCLUDE_WINDOW` (12 burn blocks after vote closes), `is-lapsed` evaluates to `true` and `get-proposal` dynamically returns `status: STATUS_EXPIRED` (3) and `reason: "not-concluded"`, leaving the stored map untouched.

---

## Question 3: Conclude Passing Paths & Payout Timing

### 3.1 The Two Passing Paths
1. **Path 1 — Tradeable Share Transfer (`"paid-shares"`)** (L675–L702):
   - **Trigger**: `(is-market-tradeable)` is `true` (market status is `MARKET_OPEN` and `burn-block-height <= close-height`).
   - **Effect**: Calls `transfer-shares` on the market contract to move 3,000 `SIDE_BONDED` shares directly from the vault to the proposer's address.

2. **Path 2 — Frozen Market Credit (`"credited"`)** (L703–L726):
   - **Trigger**: `(is-market-tradeable)` is `false` (market is resolved or deadline has passed).
   - **Effect**: Shares cannot be transferred on a frozen market. Instead, `Credits[proposer]` is incremented by 3,000 and `TotalCredits` is incremented by 3,000.

### 3.2 Value Receipt Implications
Under **Path 1**, the proposer receives liquid market shares immediately upon `conclude` execution.  
Under **Path 2**, the proposer receives a non-transferable credit entry. The proposer receives actual value (sBTC) only after `redeem-vault` (L746) is called post-resolution, which converts leftover vault shares into sBTC and allows credit holders to claim their pro-rata sBTC payout.

---

## Question 4: Timing Parameters & Real Expired Proposal Proof

### 4.1 Four Timing Parameters (Burn Blocks)
1. `VOTE_DELAY = u2` (L47): 2 burn blocks between proposal creation (`createdAt`) and voting start (`votableAtOpen`).
2. `VOTE_WINDOW = u30` (L48): 30 burn blocks for voting duration (`voteEnd = createdAt + VOTE_DELAY + VOTE_WINDOW = createdAt + 32`).
3. `CONCLUDE_WINDOW = u12` (L49): 12 burn blocks after `voteEnd` during which `conclude` must be called.
4. `GLOBAL_PROPOSE_INTERVAL = u6` (L57) / `PROPOSER_COOLDOWN = u144` (L63): 6 burn blocks global gap between any proposals / 144 burn blocks (~24h) cooldown for a single proposer.

### 4.2 Expired Proposal Behavior
If a winning proposal is not concluded within `CONCLUDE_WINDOW` (12 burn blocks after `voteEnd`), `conclude` reverts with `ERR_CONCLUDE_WINDOW_PASSED` (`u435`, L656–L658). The proposal can never be concluded, no shares/credits are paid out, and `get-proposal` reports `status: STATUS_EXPIRED` (3) with `reason: "not-concluded"`.

### 4.3 Real On-Chain Expired Proposal Citation
- **Contract**: `SP5Y3W3F78NKFH4HYFNDQMJC484VZWKDH35ZR2M9.elsalvador-yes-legion-v2`
- **Proposal ID**: `1`
- **On-Chain Data** (verified via Hiro `/v2/contracts/call-read`):
  - `createdAt`: `966,528` (`0x0ebf80`)
  - `voteEnd`: `966,560` (`0x0ebfa0`)
  - `yesVoterCount`: `5` (unanimous 5-of-5 YES votes, threshold & voters met)
  - `noWeight`: `0`
  - `status`: `3` (`STATUS_EXPIRED`)
  - `reason`: `"not-concluded"`

---

## Question 5: Anti-Takeover & Single-Holder Pass Prevention

Two independent mechanisms prevent a single holder from passing their own proposal, regardless of share count:

1. **Headcount Gate (`MIN_VOTERS = u2`)** (L85, L641):
   - `conclude` enforces `(>= (get yesVoterCount p) MIN_VOTERS)`.
   - `vote` enforces `(asserts! (not (is-eq voter (get proposer p))) ERR_SELF_VOTE)` (L423). A proposer cannot vote on their own proposal, so a single holder needs at least 1 distinct co-voter holding `>= MIN_POSITION` (1,000 shares) to vote YES.

2. **Proposer Cooldown & Live Proposal Limits** (L63, L121, L134, L649):
   - `PROPOSER_COOLDOWN = u144` (144 burn blocks) prevents an attacker from rapidly submitting proposals.
   - `ERR_HAS_LIVE_PROPOSAL` (L121) limits each proposer to 1 active proposal at a time.
   - `stillHolding` check in `conclude` (L649): `(>= (get-weight proposer) MIN_POSITION)` requires the proposer to maintain their position throughout the entire voting & conclude window. Selling shares before `conclude` triggers `"not-holding"` failure.
