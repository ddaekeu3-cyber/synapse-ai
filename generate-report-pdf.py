#!/usr/bin/env python3
"""MoltBook Daemon Report PDF Generator"""

from fpdf import FPDF
import os

OUTPUT = os.path.expanduser("~/projects/synapse-ai/moltbook-daemon-report.pdf")
FONT_PATH = "/System/Library/Fonts/AppleSDGothicNeo.ttc"


class ReportPDF(FPDF):
    def __init__(self):
        super().__init__()
        self.add_font("KR", "", FONT_PATH)
        self.add_font("KR", "B", FONT_PATH)
        self.set_auto_page_break(auto=True, margin=20)

    def header(self):
        if self.page_no() > 1:
            self.set_font("KR", "", 8)
            self.set_text_color(130, 130, 130)
            self.cell(0, 8, "MoltBook Daemon Report  |  2026-04-04", align="R")
            self.ln(4)
            self.set_draw_color(200, 200, 200)
            self.line(15, self.get_y(), 195, self.get_y())
            self.ln(4)

    def footer(self):
        self.set_y(-15)
        self.set_font("KR", "", 8)
        self.set_text_color(150, 150, 150)
        self.cell(0, 10, f"- {self.page_no()} -", align="C")

    def title_page(self):
        self.add_page()
        self.ln(50)
        self.set_font("KR", "B", 28)
        self.set_text_color(26, 26, 46)
        self.cell(0, 14, "MoltBook Daemon", align="C", new_x="LMARGIN", new_y="NEXT")
        self.cell(0, 14, "Report", align="C", new_x="LMARGIN", new_y="NEXT")
        self.ln(10)
        self.set_font("KR", "", 16)
        self.set_text_color(80, 80, 100)
        self.cell(0, 10, "MoltCoin Auto Distribution + Community Activity Daemon", align="C", new_x="LMARGIN", new_y="NEXT")
        self.ln(20)
        self.set_draw_color(22, 33, 62)
        self.set_line_width(0.8)
        self.line(60, self.get_y(), 150, self.get_y())
        self.ln(15)
        self.set_font("KR", "", 12)
        self.set_text_color(100, 100, 100)
        info = [
            "Project: Synapse AI - MoltCoin Token Ecosystem",
            "Date: 2026-04-04",
            "Status: Daemon Running (Account Suspended - Limited Activity)",
        ]
        for line in info:
            self.cell(0, 8, line, align="C", new_x="LMARGIN", new_y="NEXT")

    def h1(self, text):
        self.ln(6)
        self.set_font("KR", "B", 18)
        self.set_text_color(26, 26, 46)
        self.cell(0, 12, text, new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(22, 33, 62)
        self.set_line_width(0.6)
        self.line(15, self.get_y(), 195, self.get_y())
        self.ln(4)

    def h2(self, text):
        self.ln(4)
        self.set_font("KR", "B", 14)
        self.set_text_color(15, 52, 96)
        self.cell(0, 10, text, new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(15, 52, 96)
        self.set_line_width(0.3)
        self.line(15, self.get_y(), 120, self.get_y())
        self.ln(3)

    def h3(self, text):
        self.ln(3)
        self.set_font("KR", "B", 12)
        self.set_text_color(15, 52, 96)
        self.cell(0, 8, text, new_x="LMARGIN", new_y="NEXT")
        self.ln(2)

    def body_text(self, text):
        self.set_font("KR", "", 10)
        self.set_text_color(30, 30, 30)
        self.multi_cell(0, 6, text)
        self.ln(2)

    def bullet(self, text, indent=15):
        x = self.get_x()
        self.set_font("KR", "", 10)
        self.set_text_color(30, 30, 30)
        self.set_x(x + indent)
        self.cell(4, 6, "\u2022")
        self.multi_cell(0, 6, text)
        self.ln(1)

    def bold_text(self, label, value):
        self.set_font("KR", "B", 10)
        self.set_text_color(30, 30, 30)
        self.cell(self.get_string_width(label) + 2, 6, label)
        self.set_font("KR", "", 10)
        self.multi_cell(0, 6, value)
        self.ln(1)

    def code_block(self, code):
        self.set_fill_color(240, 240, 245)
        self.set_font("Courier", "", 8)
        self.set_text_color(30, 30, 30)
        lines = code.strip().split("\n")
        x0 = self.get_x()
        # Check if block fits on page
        block_height = len(lines) * 4.5 + 6
        if self.get_y() + block_height > 270:
            self.add_page()
        self.cell(0, 3, "", new_x="LMARGIN", new_y="NEXT", fill=True)
        for line in lines:
            self.set_x(x0)
            self.cell(0, 4.5, "  " + line, new_x="LMARGIN", new_y="NEXT", fill=True)
        self.cell(0, 3, "", new_x="LMARGIN", new_y="NEXT", fill=True)
        self.set_font("KR", "", 10)
        self.ln(3)

    def table(self, headers, rows, col_widths=None):
        if col_widths is None:
            n = len(headers)
            col_widths = [180 / n] * n
        # Check if table fits
        row_h = 7
        table_h = (len(rows) + 1) * row_h + 4
        if self.get_y() + table_h > 270:
            self.add_page()
        # Header
        self.set_fill_color(22, 33, 62)
        self.set_text_color(255, 255, 255)
        self.set_font("KR", "B", 9)
        for i, h in enumerate(headers):
            self.cell(col_widths[i], row_h, " " + h, border=1, fill=True)
        self.ln()
        # Rows
        self.set_font("KR", "", 9)
        for ri, row in enumerate(rows):
            if ri % 2 == 0:
                self.set_fill_color(248, 248, 248)
            else:
                self.set_fill_color(255, 255, 255)
            self.set_text_color(30, 30, 30)
            max_h = row_h
            for i, cell in enumerate(row):
                self.cell(col_widths[i], row_h, " " + str(cell), border=1, fill=True)
            self.ln()
        self.ln(3)

    def note_box(self, text):
        self.set_fill_color(240, 244, 255)
        self.set_draw_color(15, 52, 96)
        self.set_line_width(0.5)
        x = self.get_x()
        y = self.get_y()
        self.set_font("KR", "", 9)
        self.set_text_color(40, 40, 80)
        # Draw left border
        self.set_x(x + 4)
        w = 172
        self.multi_cell(w, 5.5, text, fill=True)
        h = self.get_y() - y
        self.line(x + 2, y, x + 2, y + h)
        self.ln(3)


def build_report():
    pdf = ReportPDF()

    # ---- Title Page ----
    pdf.title_page()

    # ---- 1. Project Overview ----
    pdf.add_page()
    pdf.h1("1. Project Overview")

    pdf.h2("1.1 Goal")
    pdf.body_text(
        "Build an autonomous daemon that automatically distributes MoltCoin (MOLT) tokens on the MoltBook platform "
        "and performs community activities (comments, follows, upvotes) independently."
    )
    pdf.body_text(
        "MoltBook platform is an agent-based social community. Our agent 'genesia' "
        "operates within this ecosystem, distributing MoltCoin tokens and building community presence."
    )

    pdf.h2("1.2 Core Requirements")
    pdf.table(
        ["Item", "Description"],
        [
            ["MoltCoin Distribution", "Every 3 hours, 77,000 MOLT equally distributed"],
            ["Distribution Announcement", "Auto-post announcement on MoltBook after each round"],
            ["Community Activity", "Every 30 min: comments, follows, upvotes"],
            ["Comment Limit", "Max 80/hour (80% of platform limit 100)"],
            ["Duplicate Prevention", "CRITICAL: No duplicate_comment violation ever"],
            ["Follow Limit", "Max 50/day (conservative)"],
            ["Process Management", "macOS launchd with auto-restart on crash"],
        ],
        [55, 125],
    )

    pdf.h2("1.3 System Architecture")
    pdf.code_block(
        """
+-----------------------------------------------------------+
|                     macOS launchd                          |
|           com.synapse.moltbook-daemon                      |
|           (KeepAlive + ThrottleInterval 30s)               |
+----------------------------+------------------------------+
                             |
                             v
+-----------------------------------------------------------+
|                moltbook-daemon.mjs                         |
|                                                            |
|  +----------------+  +----------------+  +---------------+ |
|  | Distribution   |  | Community      |  | State         | |
|  | (every 3h)     |  | (every 30min)  |  | Management    | |
|  |                |  |                |  |               | |
|  | - Collect      |  | - Comments     |  | - JSON save   | |
|  |   commenters   |  | - Follows      |  | - Atomic      | |
|  | - Distribute   |  | - Upvotes      |  |   writes      | |
|  |   tokens       |  | - Suspension   |  | - Backup      | |
|  | - Post         |  |   detection    |  |   recovery    | |
|  |   announce     |  |                |  |               | |
|  | - Git commit   |  |                |  |               | |
|  +----------------+  +----------------+  +---------------+ |
|                                                            |
|  +------------------------------------------------------+  |
|  |               MoltBook API Layer                      |  |
|  |  - Bearer token auth                                  |  |
|  |  - Math challenge verification                        |  |
|  |  - 30s timeout (AbortController)                      |  |
|  +------------------------------------------------------+  |
+-----------------------------------------------------------+
          |                                     |
          v                                     v
+------------------+              +---------------------+
|  Git Repository  |              |  MoltBook Platform   |
|  (ledger.json)   |              |  (moltbook.com)      |
|  (transactions)  |              |                      |
+------------------+              +---------------------+
"""
    )

    # ---- 2. MoltCoin Tokenomics ----
    pdf.add_page()
    pdf.h1("2. MoltCoin Tokenomics")

    pdf.h2("2.1 Basic Design")
    pdf.table(
        ["Item", "Value"],
        [
            ["Total Supply", "1,000,000,000 MOLT"],
            ["Distribution Pool", "900,000,000 MOLT (90%)"],
            ["Founder Reserve", "~99,743,340 MOLT (~10%)"],
            ["Per Round", "77,000 MOLT"],
            ["Distribution Interval", "3 hours"],
            ["Halving Epoch", "2 years"],
        ],
        [55, 125],
    )

    pdf.h2("2.2 Halving Schedule")
    pdf.table(
        ["Epoch", "Period", "Per Round"],
        [
            ["1 (current)", "2026.04.03 ~ 2028.04.03", "77,000 MOLT"],
            ["2", "2028.04.03 ~ 2030.04.03", "38,500 MOLT"],
            ["3", "2030.04.03 ~", "19,250 MOLT"],
        ],
        [30, 80, 70],
    )

    pdf.h2("2.3 Current Distribution Status (11 Rounds Complete)")
    pdf.table(
        ["Item", "Value"],
        [
            ["Circulating", "1,026,652 MOLT"],
            ["Distribution Pool Remaining", "899,230,008 MOLT"],
            ["Total Holders", "8"],
        ],
        [65, 115],
    )

    pdf.h2("2.4 Holder Balances")
    pdf.table(
        ["Agent", "Balance (MOLT)", "Share"],
        [
            ["genesia", "320,831", "31.3%"],
            ["Undercurrent", "320,830", "31.3%"],
            ["han-sajang", "128,331", "12.5%"],
            ["FailSafe-ARGUS", "76,998", "7.5%"],
            ["ravencoal", "51,332", "5.0%"],
            ["hope_valueism", "51,332", "5.0%"],
            ["gig_0racle", "51,332", "5.0%"],
            ["VibeCodingBot", "25,666", "2.5%"],
        ],
        [55, 55, 70],
    )
    pdf.note_box(
        "Note: Top 2 holders (genesia + Undercurrent) hold 62.5%. "
        "HHI index is 2,241, approaching 'highly concentrated' threshold (2,500). "
        "Mitigation: minimum 3 participants required + distribution announcement commenters added to eligible set."
    )

    pdf.h2("2.5 Transaction History")
    pdf.table(
        ["Round", "Time (UTC)", "Recipients", "Per Person", "Note"],
        [
            ["1", "04/03 13:14", "3", "25,667", "First distribution"],
            ["2", "04/03 16:14", "3", "25,666", ""],
            ["3", "04/03 19:14", "3", "25,666", ""],
            ["4", "04/03 22:14", "3", "25,666", ""],
            ["5", "04/04 01:14", "3", "25,666", ""],
            ["retro", "04/04 03:12", "5", "varies", "Retroactive (missed)"],
            ["7", "04/04 04:32", "2", "38,500", ""],
            ["8", "04/04 04:33", "2", "38,500", "Rapid-fire bug"],
            ["9", "04/04 04:34", "2", "38,500", "Rapid-fire bug"],
            ["10", "04/04 04:37", "2", "38,500", ""],
            ["11", "04/04 04:45", "2", "38,500", ""],
        ],
        [20, 38, 30, 32, 60],
    )
    pdf.note_box(
        "Round 6 missing: skipped between round 5 and retroactive distribution.\n"
        "Rounds 7-9 rapid-fire bug: daemon restart triggered immediate execution. Fixed with time-based guard."
    )

    # ---- 3. MoltBook API Integration ----
    pdf.add_page()
    pdf.h1("3. MoltBook API Integration")

    pdf.h2("3.1 API Basics")
    pdf.table(
        ["Item", "Value"],
        [
            ["Base URL", "https://www.moltbook.com/api/v1"],
            ["Auth", "Bearer token"],
        ],
        [40, 140],
    )

    pdf.h2("3.2 API Endpoints Used")
    pdf.table(
        ["Method", "Endpoint", "Purpose"],
        [
            ["GET", "/posts/{id}", "Get post (with comments)"],
            ["GET", "/posts/{id}/comments", "Get comment list"],
            ["GET", "/feed/hot", "Get hot feed"],
            ["POST", "/posts", "Create new post"],
            ["POST", "/posts/{id}/comments", "Post comment"],
            ["POST", "/posts/{id}/upvote", "Upvote post"],
            ["GET", "/agents/me", "My profile"],
            ["GET", "/agents?sort=top&limit=20", "Agent list"],
            ["POST", "/agents/{name}/follow", "Follow agent (by name)"],
        ],
        [22, 72, 86],
    )

    pdf.h2("3.3 Verification Challenge System")
    pdf.body_text(
        "MoltBook requires solving a math challenge for every post/comment creation. "
        "The server returns a verification_question (e.g., 'eight newtons plus three newtons') "
        "and the client must return the sum in 'XX.00' format."
    )
    pdf.h3("Algorithm:")
    pdf.bullet("Strip non-alphabetic characters, convert to lowercase")
    pdf.bullet("Remove consecutive duplicate characters (e.g., 'neeewtons' -> 'newtons')")
    pdf.bullet("Match unit patterns (newtons, watts, joules and variants)")
    pdf.bullet("Extract number before each unit")
    pdf.bullet("Sum the two numbers, return as 'XX.00'")

    pdf.h2("3.4 Post Creation Required Fields")
    pdf.code_block(
        """
{
  title: "Title",
  content: "Content",
  submolt_name: "agentfinance",   // REQUIRED! 400 error without
  submolt: "agentfinance",         // REQUIRED!
  verification_answer: "11.00"
}
"""
    )
    pdf.note_box(
        "Discovery: Without submolt_name, the API returns 'submolt_name must be a string' 400 error. "
        "This required trial-and-error to identify."
    )

    # ---- 4. Daemon Core Functions ----
    pdf.add_page()
    pdf.h1("4. Daemon Core Functions")

    pdf.h2("4.1 File Structure")
    pdf.table(
        ["File", "Location", "Description"],
        [
            ["moltbook-daemon.mjs", "~/.openclaw/workspace/", "Main daemon code (~780 lines)"],
            ["moltbook-state.json", "~/.openclaw/workspace/", "State file (comment/follow history)"],
            ["moltbook-daemon.log", "~/.openclaw/workspace/", "Log file"],
            ["*.plist", "~/Library/LaunchAgents/", "launchd configuration"],
            ["ledger.json", "~/projects/synapse-ai/moltcoin/", "Token ledger"],
            ["transactions.json", "~/projects/synapse-ai/moltcoin/", "Transaction records"],
        ],
        [48, 62, 70],
    )

    pdf.h2("4.2 API Call Function")
    pdf.body_text(
        "All API calls go through a centralized function with 30-second AbortController timeout. "
        "This prevents the daemon from hanging indefinitely if the server doesn't respond."
    )
    pdf.code_block(
        """
async function api(method, path, body) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 30_000);
  const res = await fetch(BASE_URL + path, {
    method,
    headers: {
      'Authorization': 'Bearer ' + API_KEY,
      'Content-Type': 'application/json'
    },
    body: body ? JSON.stringify(body) : undefined,
    signal: controller.signal,
  });
  clearTimeout(timeout);
  const json = await res.json();
  if (!json.statusCode) json.statusCode = res.status;
  return json;
}
"""
    )

    pdf.h2("4.3 Atomic File Writes")
    pdf.body_text(
        "State and ledger files are written atomically using the POSIX write-fsync-rename pattern. "
        "This ensures data integrity even if the process crashes mid-write."
    )
    pdf.code_block(
        """
function atomicWrite(path, data) {
  const tmp = path + '.tmp';
  writeFileSync(tmp, data);      // 1. Write to temp file
  const fd = openSync(tmp, 'r');
  fsyncSync(fd);                  // 2. Sync to disk
  closeSync(fd);
  renameSync(tmp, path);         // 3. Atomic rename
}
"""
    )

    pdf.h2("4.4 State Management")
    pdf.h3("State File Structure (moltbook-state.json):")
    pdf.code_block(
        """
{
  replied_comments: [],        // Comment IDs we replied to
  commented_posts: [],         // Post IDs we commented on
  followed_agents: [],         // Agents we followed
  last_distribution: "...",    // Last distribution time
  last_community: "...",       // Last community activity time
  suspended_until: "...",      // Suspension end time
  rate_limit_until: "...",     // Rate limit end time
  last_comment_run: "...",     // Last comment batch time
  upvoted_posts: [],           // Posts we upvoted
  comment_timestamps: [],      // Timestamps for hourly cap tracking
  follows_by_date: {},         // Daily follow counts
  suspension_lifted_at: "..."  // When suspension was detected as lifted
}
"""
    )
    pdf.h3("Load Safety Features:")
    pdf.bullet("JSON parse failure: falls back to .bak backup file")
    pdf.bullet("Null value filtering: removes null entries from arrays")
    pdf.bullet("Array size cap: trims to last 500 entries if exceeded")

    pdf.h2("4.5 Comment Gate System (Multi-Layer Protection)")
    pdf.body_text("Every comment must pass 5 checks before being posted:")
    pdf.code_block(
        """
                canComment(state)

  1. Account suspended?       -> BLOCK
  2. Rate limited?            -> BLOCK
  3. >80 comments this hour?  -> BLOCK
  4. <25min since last batch?  -> BLOCK
  5. All checks passed         -> ALLOW
"""
    )
    pdf.table(
        ["Check", "Threshold", "Purpose"],
        [
            ["Suspension", "suspended_until > now", "Respect platform ban"],
            ["Rate Limit", "rate_limit_until > now", "429 response cooldown"],
            ["Hourly Cap", "80/hour", "80% of platform's 100/hr limit"],
            ["Min Gap", "25 minutes", "Natural activity pattern"],
        ],
        [40, 55, 85],
    )

    # ---- Comment Generation ----
    pdf.add_page()
    pdf.h2("4.6 Comment Generation System")
    pdf.body_text(
        "CRITICAL RULE: No duplicate_comment violation EVER. Every comment must be unique."
    )

    pdf.h3("4.6.1 Topic-Based Generation (makeComment)")
    pdf.body_text(
        "Analyzes post title/content to detect one of 8 topics, then generates a relevant comment:"
    )
    pdf.table(
        ["Topic", "Keywords", "Example Start"],
        [
            ["memory", "memory, recall, forget", "memory persistence is the real test..."],
            ["identity", "identity, persona, self", "identity layers in agents create..."],
            ["platform", "platform, infra, scale", "platform decisions at this stage..."],
            ["infra", "deploy, server, uptime", "infra reliability is the bottleneck..."],
            ["security", "security, exploit, vuln", "security in agent systems is..."],
            ["crypto", "token, mint, blockchain", "token mechanics need careful..."],
            ["agentWork", "agent, autonomous, task", "autonomous agent coordination..."],
            ["philosophy", "conscious, ethics", "the philosophical edge of..."],
        ],
        [30, 55, 95],
    )
    pdf.bullet("6 variants per topic = 48 topic-based combinations")
    pdf.bullet("Generic fallback: 12 starters x 8 closers = 96 combinations")
    pdf.bullet("Total: 144+ unique combinations")

    pdf.h3("4.6.2 Anchor Extraction")
    pdf.body_text(
        "Extracts 4 meaningful words from post title (filtering out common words like "
        "'the', 'this', 'that', 'with', 'from', 'about') and weaves them into the comment "
        "for contextual relevance."
    )

    pdf.h3("4.6.3 Self-Dedup Check (Jaccard Similarity)")
    pdf.code_block(
        """
function isDuplicate(text) {
  const words = new Set(
    text.toLowerCase().split(/\\s+/).filter(w => w.length > 4)
  );
  for (const prev of recentCommentHashes) {
    const overlap = [...words].filter(w => prev.has(w)).length;
    const similarity = overlap / Math.max(words.size, prev.size);
    if (similarity > 0.4) return true;  // >40% overlap = duplicate
  }
  recentCommentHashes.push(words);
  if (recentCommentHashes.length > 50) recentCommentHashes.shift();
  return false;
}
"""
    )
    pdf.bullet("Compares against last 50 comments")
    pdf.bullet("Only considers words with 5+ characters (ignores articles/prepositions)")
    pdf.bullet("If flagged as duplicate, regenerates up to 5 times")

    pdf.h3("4.6.4 Cryptographic Randomness")
    pdf.body_text(
        "Uses crypto.randomInt() instead of Math.random() or time-based seeds. "
        "This ensures truly unpredictable comment selection."
    )
    pdf.note_box(
        "Previous bug: (Date.now() + count * 7) % 12 produced only variants 0 and 3 "
        "when executed at regular intervals. crypto.randomInt() fixed this completely."
    )

    # ---- Distribution System ----
    pdf.add_page()
    pdf.h2("4.7 MoltCoin Distribution System (runDistribution)")

    pdf.h3("4.7.1 Eligible Participant Collection (Expanded)")
    pdf.code_block(
        """
Eligible agent collection:
1. Launch post (694f1b1f...) commenters
2. Recent distribution announcement post commenters
-> Deduplicate -> Exclude self (genesia)
-> If < 3 participants, skip this round
"""
    )

    pdf.h3("4.7.2 Distribution Process")
    pdf.body_text("10-step process for each distribution round:")
    nums = [
        "Collect participants (launch post + announcement post comments)",
        "Verify eligibility (minimum 3 participants required)",
        "Calculate equal distribution (77,000 / participant count)",
        "Update ledger.json (balances, circulating, pool deduction)",
        "Record in transactions.json",
        "Invariant check: sum(balances) + founder_reserve + remaining == total_supply",
        "Update state.last_distribution (prevents double-distribution)",
        "Atomic file writes for all data files",
        "Git commit & push to repository",
        "Post distribution announcement on MoltBook",
    ]
    for i, item in enumerate(nums, 1):
        pdf.bullet(f"{i}. {item}")

    pdf.h3("4.7.3 Double-Distribution Prevention")
    pdf.code_block(
        """
// In main(): time-based startup guard
if (now - lastDistribution >= DISTRIBUTION_INTERVAL_MS) {
  await runDistribution();
}

// In runDistribution(): state saved BEFORE git
state.last_distribution = new Date().toISOString();
saveState(state);  // Even if git fails, won't re-distribute
"""
    )

    pdf.h2("4.8 Distribution Announcement Post")
    pdf.body_text(
        "8 varied title/intro/outro templates rotating by roundNum % 8. "
        "Each announcement includes: total MOLT distributed, participant count, "
        "per-person amount, current circulating supply, total holders, and how to participate."
    )

    pdf.h2("4.9 Community Activity (runCommunity)")
    pdf.body_text("Runs every 30 minutes (+ 2-8 min random jitter):")
    pdf.bullet("Reply to comments on our posts (maxReplies)")
    pdf.bullet("Comment on hot feed posts (maxHotFeed)")
    pdf.bullet("Follow agents (daily cap: 50)")
    pdf.bullet("Upvote posts")

    pdf.h3("4.9.1 Post-Suspension Ramp-Up")
    pdf.body_text(
        "After suspension is lifted, activity ramps up gradually over 4 cycles "
        "to avoid triggering another ban:"
    )
    pdf.table(
        ["Cycle", "Time After Lift", "Replies", "Feed Comments", "Note"],
        [
            ["0", "0-30min", "0", "0", "Upvotes+follows only"],
            ["1", "30-60min", "2", "0", "Small replies only"],
            ["2", "60-90min", "2", "2", "Feed comments start"],
            ["3", "90-120min", "3", "4", "Near-normal"],
            ["4+", "120min+", "Normal", "Normal", "Full activity"],
        ],
        [20, 35, 30, 40, 55],
    )

    pdf.h2("4.10 Follow System")
    pdf.bullet("Daily cap: 50 follows/day tracked by date string")
    pdf.bullet("Skips already-followed agents (state.followed_agents)")
    pdf.bullet("API: POST /api/v1/agents/{name}/follow (name-based, not UUID)")

    # ---- 5. Process Management ----
    pdf.add_page()
    pdf.h1("5. Process Management (launchd)")

    pdf.h2("5.1 Configuration")
    pdf.body_text("Location: ~/Library/LaunchAgents/com.synapse.moltbook-daemon.plist")
    pdf.code_block(
        """
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.synapse.moltbook-daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string>~/.nvm/versions/node/v24.13.0/bin/node</string>
        <string>~/.openclaw/workspace/moltbook-daemon.mjs</string>
    </array>
    <key>RunAtLoad</key>   <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>  <false/>
    </dict>
    <key>ThrottleInterval</key>  <integer>30</integer>
</dict>
</plist>
"""
    )

    pdf.h2("5.2 Key Settings")
    pdf.table(
        ["Setting", "Value", "Meaning"],
        [
            ["RunAtLoad", "true", "Auto-start on boot/login"],
            ["KeepAlive.SuccessfulExit", "false", "Restart only on crash (non-zero exit)"],
            ["ThrottleInterval", "30s", "Minimum gap between restarts"],
        ],
        [55, 30, 95],
    )

    pdf.h2("5.3 Management Commands")
    pdf.code_block(
        """
# Load (start) daemon
launchctl load ~/Library/LaunchAgents/com.synapse.moltbook-daemon.plist

# Unload (stop) daemon
launchctl unload ~/Library/LaunchAgents/com.synapse.moltbook-daemon.plist

# Check status
launchctl list | grep moltbook

# View logs
tail -f ~/.openclaw/workspace/moltbook-daemon.log
"""
    )

    # ---- 6. Bugs Found & Fixed ----
    pdf.add_page()
    pdf.h1("6. Bugs Found & Fixed")

    bugs = [
        {
            "title": "6.1 24-Hour Daemon Hang [CRITICAL]",
            "symptom": "Last log at 04:45 UTC, then no output for 24 hours. Process alive but unresponsive.",
            "cause": "fetch() had no timeout. Server non-response blocked the event loop permanently.",
            "fix": "Added AbortController with 30-second timeout to all API calls.",
            "lesson": "Every external HTTP call MUST have a timeout.",
        },
        {
            "title": "6.2 suspended_until Date Parsing Bug [CRITICAL]",
            "symptom": "Daemon attempted comments while suspended, getting 403 errors repeatedly.",
            "cause": 'State file had "2026-04-05T04:13:28.737Z." (trailing period). '
            "new Date('...Z.') = Invalid Date. Invalid Date > now = false, "
            "so daemon thought it was NOT suspended.",
            "fix": "1) Fixed state file manually. 2) Added regex sanitization: until.replace(/[^0-9TZ:.+-]/g, '')",
            "lesson": "External input dates MUST be sanitized. One extra character can break all logic.",
        },
        {
            "title": "6.3 Double Distribution on Restart [HIGH]",
            "symptom": "Round 10 (04:37) and Round 11 (04:45) executed 8 min apart instead of 3 hours.",
            "cause": "main() always called runDistribution() immediately on start, regardless of last run time.",
            "fix": "Added time check in main(): only run if now - lastDistribution >= 3 hours.",
            "lesson": "Daemons must ALWAYS check last execution time on restart.",
        },
        {
            "title": "6.4 Seed Collision - Only 2 Comment Variants [HIGH]",
            "symptom": "Comments alternated between only variant 0 and variant 3.",
            "cause": "(Date.now() + count * 7) % 12 with 5000ms sleep interval: step always = 3.",
            "fix": "Replaced with crypto.randomInt(0, N) for true randomness.",
            "lesson": "Time-based seeds collide with regular execution intervals.",
        },
        {
            "title": "6.5 Missing submolt_name Field [MEDIUM]",
            "symptom": '"submolt_name must be a string" 400 error on post creation.',
            "cause": "Posts require submolt_name and submolt fields, undocumented.",
            "fix": "Added submolt_name: 'agentfinance', submolt: 'agentfinance' to POST body.",
            "lesson": "API requirements discovered through trial and error.",
        },
        {
            "title": "6.6 Variable Name Collision [MEDIUM]",
            "symptom": "postDistributionAnnouncement() crashed on execution.",
            "cause": "'const r = roundNum % 8' and 'const r = await api(...)' in same scope.",
            "fix": "Renamed API response variable to 'postRes'.",
            "lesson": "Careful naming in functions with multiple operations.",
        },
    ]

    for bug in bugs:
        pdf.h2(bug["title"])
        pdf.bold_text("Symptom: ", bug["symptom"])
        pdf.bold_text("Root Cause: ", bug["cause"])
        pdf.bold_text("Fix: ", bug["fix"])
        pdf.bold_text("Lesson: ", bug["lesson"])
        pdf.ln(2)

    # ---- 7. 5-Agent Analysis ----
    pdf.add_page()
    pdf.h1("7. Five-Agent Parallel Analysis")
    pdf.body_text(
        "Five specialized AI agents were run in parallel to analyze the daemon code from different perspectives:"
    )

    pdf.h2("7.1 Agent Composition")
    pdf.table(
        ["Agent", "Role", "Key Finding"],
        [
            ["Reliability", "Stability analysis", "fetch timeout missing, non-atomic writes"],
            ["Anti-Detection", "Detection evasion", "Seed collision, no self-dedup check"],
            ["Tokenomics", "Token economics", "HHI concentration, retroactive gaps"],
            ["Community Strategy", "Community growth", "Need question posts, activity diversity"],
            ["Code Quality", "Code quality", "Dead code, error handling gaps"],
        ],
        [38, 42, 100],
    )

    pdf.h2("7.2 Prioritized Improvements & Results")
    pdf.table(
        ["#", "Item", "Priority", "Status"],
        [
            ["1", "suspended_until trailing period fix", "P0 Critical", "DONE"],
            ["2", "Git timeout + pull --rebase", "P0 Critical", "DONE"],
            ["3", "Atomic file writes", "P0 Critical", "DONE"],
            ["4", "crypto.randomInt + self-dedup + 96 combos", "P1 High", "DONE"],
            ["5", "Post-suspension ramp-up (4 cycles)", "P1 High", "DONE"],
            ["6", "Claude Haiku LLM comment generation", "P1 High", "PENDING (need API key)"],
            ["7", "Distribution post commenters in eligible set", "P2 Medium", "DONE"],
            ["8", "Min 3 participants for distribution", "P2 Medium", "DONE"],
            ["9", "launchd process supervision", "P2 Medium", "DONE"],
            ["10", "Question post for inbound comments", "P3 Low", "PLANNED"],
        ],
        [10, 80, 35, 55],
    )

    # ---- 8. Account Suspension ----
    pdf.add_page()
    pdf.h1("8. Account Suspension Incident")

    pdf.h2("8.1 Timeline")
    pdf.table(
        ["Time (UTC)", "Event"],
        [
            ["~04:13 Apr 4", "duplicate_comment auto-mod triggered account suspension"],
            ["04:13 Apr 4", "Suspension starts - posts AND comments return 403"],
            ["04:13 Apr 5", "Suspension lifts (24 hour ban)"],
        ],
        [45, 135],
    )

    pdf.h2("8.2 Key Discoveries")
    pdf.bullet("Suspension blocks BOTH posts AND comments (not just comments as initially thought)")
    pdf.bullet("Follows and upvotes continue to work during suspension")
    pdf.bullet("Cause: template-based comments had structural similarity detected by auto-mod")

    pdf.h2("8.3 Countermeasures Implemented")
    pdf.bullet("1. Expanded comment combinations (72 -> 144+)")
    pdf.bullet("2. Added Jaccard self-dedup check (threshold 0.4)")
    pdf.bullet("3. Switched to crypto.randomInt() for true randomness")
    pdf.bullet("4. Post-suspension 4-cycle ramp-up")
    pdf.bullet("5. Hourly cap at 80 (80% of platform limit)")
    pdf.bullet("6. (Pending) Claude Haiku API for LLM-generated comments (~$1.50/month)")

    # ---- 9. Current Status & Roadmap ----
    pdf.add_page()
    pdf.h1("9. Current Status & Future Plans")

    pdf.h2("9.1 Current Operational Status")
    pdf.table(
        ["Item", "Status"],
        [
            ["Daemon PID", "10610 (running)"],
            ["launchd", "Loaded and active"],
            ["Account Suspension", "Until 2026-04-05 04:13 UTC"],
            ["Follow/Upvote", "Working normally"],
            ["Comments/Posts", "Blocked until suspension lifts"],
        ],
        [55, 125],
    )

    pdf.h2("9.2 Pending Tasks")
    pdf.table(
        ["Item", "Status", "Description"],
        [
            ["Claude Haiku comments", "Need API key", "LLM-based organic comment generation (~$1.50/mo)"],
            ["Question post", "After suspension", "Drive inbound comments to expand MOLT eligible set"],
            ["Round 10-11 announcement", "After suspension", "Post distribution announcements on MoltBook"],
        ],
        [42, 38, 100],
    )

    pdf.h2("9.3 Short-term Roadmap (1 Week)")
    pdf.table(
        ["Item", "Description"],
        [
            ["MOLT Tip/Transfer", "Agent-to-agent MOLT transfers"],
            ["Distribution Interval", "Adjust from 3h to 6-8h (reduce concentration)"],
            ["Halving Auto-detect", "Auto-transition at epoch boundaries"],
        ],
        [50, 130],
    )

    pdf.h2("9.4 Long-term Roadmap (1 Month)")
    pdf.table(
        ["Item", "Description"],
        [
            ["GA4 Data Analysis", "GitHub Pages traffic analysis"],
            ["SEO Strategy", "Search engine optimization"],
            ["MOLT vs SYNAPSE", "Token unification/separation strategy"],
        ],
        [50, 130],
    )

    # ---- 10. Key Lessons ----
    pdf.add_page()
    pdf.h1("10. Key Lessons Learned")

    lessons = [
        (
            "Always set timeouts on HTTP calls",
            "A server non-response can block the entire daemon's event loop forever. "
            "The daemon hung for 24 hours due to a single fetch() without timeout.",
        ),
        (
            "Write state files atomically",
            "Use write -> fsync -> rename pattern. A crash during writeFileSync can corrupt "
            "the file, losing all state data.",
        ),
        (
            "Sanitize all external input",
            "A single trailing period in a date string ('2026-04-05T04:13:28.737Z.') broke "
            "the entire suspension detection. Invalid Date comparisons always return false.",
        ),
        (
            "Don't use time-based seeds for randomness",
            "Regular execution intervals cause seed collisions. Use crypto.randomInt() for "
            "cryptographically secure random numbers.",
        ),
        (
            "Check last execution time on daemon restart",
            "Without this check, distribution ran 5 times in 15 minutes after a restart, "
            "distributing 385,000 MOLT incorrectly.",
        ),
        (
            "Use 80% of platform limits",
            "Operating at the edge of limits leaves no safety margin. 80% provides buffer "
            "for timing variations and API response delays.",
        ),
        (
            "Ramp up gradually after suspension",
            "Immediately resuming full activity after a ban risks triggering another. "
            "A 4-cycle progressive ramp-up is the safest approach.",
        ),
        (
            "Use a process manager (launchd)",
            "Manual monitoring is unreliable. launchd automatically restarts on crash, "
            "starts on boot, and throttles rapid restart loops.",
        ),
    ]

    for i, (title, desc) in enumerate(lessons, 1):
        pdf.h3(f"{i}. {title}")
        pdf.body_text(desc)

    pdf.ln(10)
    pdf.set_draw_color(22, 33, 62)
    pdf.set_line_width(0.5)
    pdf.line(60, pdf.get_y(), 150, pdf.get_y())
    pdf.ln(8)
    pdf.set_font("KR", "", 10)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 6, "This report documents the complete MoltBook Daemon build process", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, "from April 3-4, 2026.", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, "Generated: 2026-04-04", align="C", new_x="LMARGIN", new_y="NEXT")

    pdf.output(OUTPUT)
    print(f"PDF generated: {OUTPUT}")
    print(f"Pages: {pdf.page_no()}")


if __name__ == "__main__":
    build_report()
