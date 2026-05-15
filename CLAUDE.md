# SST3 Solo Workflow

## 5-Stage Solo Workflow Model

**Your Role**: Orchestrate research/review via subagent swarms; implement directly. See `../dotfiles/SST3/workflow/WORKFLOW.md` for full 5-stage workflow.

**Default: PLANNING MODE** — execute only when user says "work on #X" / "implement this". No file changes, no commits in planning mode. When unclear, ask.

**MANDATORY READING**:
1. `../dotfiles/SST3/standards/STANDARDS.md` (ALWAYS)
2. `../dotfiles/SST3/standards/ANTI-PATTERNS.md` (ALWAYS — 19 documented failure modes you must not repeat)
3. `{repository-name}/CLAUDE.md` (ALWAYS - replace with repo root)

**Reading Confirmation Checklist** (MUST display and complete):
- [ ] Read STANDARDS.md
- [ ] Read ANTI-PATTERNS.md
- [ ] Read {repository-name}/CLAUDE.md

**Critical behavioural rules** (full detail in STANDARDS.md + ANTI-PATTERNS.md):
- **GREP BEFORE WRITING/CODING**: before creating ANY new file, rule, memory, helper, hook, harness, function, class, component, workflow, process, design, or piece of logic — grep relevant directories with multiple synonyms. Update existing in place if found. New files only after grep confirms nothing exists. (AP #10)
- **MULTI-LAYER SUBAGENT DISCIPLINE** (AP #14): never stingy. Subagent count is DYNAMIC, scaled to cover every directory / file / claim category line-by-line — no stone left unturned. NOT 2-3 as a default. If the work has 12 claim categories, dispatch ≥12 subagents. Use LAYERS cross-checking each other from DIFFERENT angles (layer 2 ≠ layer 1 prompt). Main agent VERIFIES every subagent finding against source — never assume the subagent got it right. Every claim must be factually provable AND the proof method must be documented inline so future audits don't false-positive on it.
- **AP #9 Single-Source Edits**: every edit to a multi-research artefact must integrate ALL relevant sources in the same pass. Never apply one in isolation.
- **AP #11 Stopping vs Applying**: when an audit surfaces a documented violation, RUN the full process (false-positive sweep then apply). Don't stop to ask permission for fixes the standards already mandate. Don't apply without the sweep.
- **AP #12 No Observability**: every component needs structured logs, metrics, and audit trails AT WRITE TIME. Not after the first incident.
- **AP #13 "Proceed" ≠ "Bypass Process"**: when the user says okay / proceed / yes / go ahead, that means **proceed using the full standard process** — not skip the sweeps, gates, Ralph reviews, or guardrails. User authorisation never bypasses workflow.
- **AP #17 Keep Going Until Done**: do NOT stop mid-work to ask permission, wait for user confirmation, or "check in". Phase checkpoints post a comment to the Issue and CONTINUE. Stop ONLY for: (a) context at 80%+ of model window, (b) irreversible destructive action needing user consent (force-push, rm -rf, DROP TABLE, branch deletion), (c) genuinely stuck after investigation (not a first-response-to-friction reflex), (d) task complete. Warn at 70%, keep working until 80%. The 1M window exists to be used.
- **AP #16 Monitor, Don't Fire-and-Forget**: every script / command / subprocess / test / deployment / commit / push you launch must be verified end-to-end (tail logs, check exit code, verify output, confirm side effects). "Started" is not "done". For `run_in_background`, poll BashOutput. Be the user's eyes and ears, not just their executioner. If you cannot answer "what happened?" with specifics, you fired and forgot — go check NOW.
- **AP #18 Sample Invocation Validates Workflow**: for any change touching pipeline / backtest / SL1 / SL2 / orchestration / CLI-wiring / cross-module function-arg propagation — run an actual end-to-end sample invocation (real CLI, real DB, small liquid basket 8 tickers) BEFORE closing. Unit + smoke tests are necessary but NOT sufficient. Mocks that accept `**kwargs` silently discard params and do NOT prove propagation — assert `call_args.kwargs[...]` explicitly. Stage 4 Verification Loop mandatory gate. See STANDARDS.md "Testing Priority — Workflow Validation Gate".
- **Per-Stage Feedback Capture** (canonical: STANDARDS.md §Per-Stage Feedback Capture). Write `dotfiles/SST3-metrics/leader-feedback/feedback-<repo>-<issue>.md` `## Stage N` block at each `/Leader` stage close. 10 fields per stage (model / worked / didnt / why / improvement / improvement_status / evidence / friction / rule_self_caught / rule_user_caught). Channel rule (forward-preference-blocklist enforced via pre-commit hook `sst3-metrics-feedback-present`): feedback files MUST NOT contain `prefers / always / from now on / default ON / going forward` phrasing — that's auto-memory's channel; attribution wording (`Hoi flagged`, `user pointed out`) is FINE.

**STOP if**: No GitHub Issue exists. Create Issue using `../dotfiles/SST3/templates/issue-template.md`.

### Solo Workflow Overview

**Context Window**: 1M tokens (Opus 4.6/Sonnet 4.6), 200K (Haiku 4.5)
**Content Budget**: ~42K tokens (STANDARDS.md + CLAUDE.md + Issue loaded at session start)
**Handover at**: 80% of model window (800K for 1M, 160K for Haiku) — STOP threshold, not routine. Warn at 70%. Keep working until 80%.
**Issue Header**: `## Solo Assignment (SST3 Automated)`
**Branch**: `solo/issue-{number}-{description}` (commit per file, no PR)
**Merge**: Direct merge to main after Ralph Review passes (BEFORE user review - protects work)

### Execution Guardrails (Built-in)

Pre-start read (CLAUDE.md + STANDARDS.md + Issue) → phase checkpoints (70%+ warn, 80%+ STOP) → post-compact re-read → verification loop until clean → user-review-checklist.md.

### Branch Safety (CRITICAL — DO NOT VIOLATE)

- **NEVER switch branches** (`git checkout main`, `git checkout -b`, `git switch`).
- **Always commit and push to the CURRENT active branch** — it will get merged later.
- If you need something on main, **ask the user** — do NOT switch yourself.
- The only exception is creating a NEW solo branch at the START of work.

### Command Interface

- `/start` — list repos, prompt selection, load CLAUDE.md, WAIT for task.
- `/SST3-solo` — load STANDARDS.md + repo CLAUDE.md, display summary, prompt for task, execute with guardrails.

Handover template: `../dotfiles/SST3/templates/chat-handover.md` (post checkpoint to Issue FIRST).

## External Research References

**Location**: `docs/research/` in project root
**Check BEFORE external research**: Existing research references
**Capture AFTER research**: If 3+ external resources found, create/update research reference
See: `../dotfiles/SST3/reference/research-reference-guide.md` for complete guide

## Quality Standards

**See STANDARDS.md** — Never Assume (read source before concluding), Fix Everything (no scope/language excuses, no priority deferrals), Critical Thinking (challenge with evidence). Only valid skip reason: confirmed false positive (document why).

**Voice Content Protection** — when editing Hoi-voice prose (CV, LinkedIn, cover letters, blogs): wrap in `<!-- iamhoi --> ... <!-- iamhoiend -->`. Canonical rules in `../dotfiles/SST3/standards/STANDARDS.md` "Voice Content Protection" + AP #15. Single source of truth for banned words: `../dotfiles/SST3/scripts/voice_rules.py`. (#406 F3.8 dedup.)

## Ralph Review Loop (MANDATORY)

**Subagents are PLANNING ONLY** - they review, they do NOT write code.

**Flow**: Implement → Haiku → Sonnet → Opus → **Merge to main** → User Review

| Tier | Model | Purpose | Invocation |
|------|-------|---------|------------|
| 1 | `haiku` (MANDATORY) | Surface checks | `Task(model=haiku, prompt="Review per SST3/ralph/haiku-review.md...")` |
| 2 | `sonnet` (MANDATORY) | Logic checks | `Task(model=sonnet, prompt="Review per SST3/ralph/sonnet-review.md...")` |
| 3 | `opus` (MANDATORY) | Deep analysis | `Task(model=opus, prompt="Review per SST3/ralph/opus-review.md...")` |

**On FAIL any tier**: Main agent fixes → Restart from Tier 1 (Haiku)
**On PASS all 3**: Merge to main immediately (protects work), then user review

**Checklists**: `../dotfiles/SST3/ralph/`

## Quick Reference

### 5-Stage Workflow (ORDER-DEPENDENT — no skipping, no reordering)
```
Stage 1: Research — subagent swarm → main agent writes /tmp (findings + gaps + plan)
Stage 2: Issue Creation — main agent from /tmp, illustrations, compact breaks, quality mantras verbatim
Stage 3: Triple-Check — subagents verify scope vs audit = 100%, chat history, dead code
Stage 4: Implementation — main agent implements, Verification Loop, Ralph Review, merge, user-review-checklist
Stage 5: Post-Implementation Review — subagent swarm: wiring, goal alignment, quality scan, regression tests + completeness gate (Layer A pre-flight `bash SST3/scripts/leader-stage5-completeness-check.sh <issue>` + Layer B post-flight failsafe `.github/workflows/stage5-completeness.yml`; both mandatory, neither replaces the other; #460 W4)
```

### Solo Execution Checklist (Stage 4)
```
## Working on Issue #X
Read CLAUDE.md, STANDARDS.md, Issue
Create branch: git checkout -b solo/issue-{X}-{description}
Execute phase 1, commit per file, push, post checkpoint
Execute phase 2, commit per file, push, post checkpoint
...
Run verification loop until clean (overengineering, reuse, duplication, fallbacks, wiring, regression, quality)
Run Ralph Review (Haiku → Sonnet → Opus)
Merge to main (BEFORE user review - protects work, check for conflicts first)
Post user-review-checklist.md (from TEMPLATE, ALL sections mandatory)
User reviews and approves
Cleanup branch, close Issue
```

### Emergency Procedures
- **Context overflow**: Create handover immediately
- **Stuck**: Re-read Issue, identify blocker, post to Issue
- **User compact**: Re-read CLAUDE.md, STANDARDS.md, Issue last comment

### MCP Configuration (Global)
- **Location**: `~/.claude.json` (user scope)
- **Verify**: Run `claude mcp list` or `/mcp` inside Claude Code
- **Servers**: chrome-devtools, github-checkbox, github
- **Wrapper-lane (Issue #445; #447 Phase 6+8 expansion)**: Stateless, request-scoped bash wrappers across 4 phases — no daemon, no SQLite, no persistent graph. Invoked via 40 scripts in `dotfiles/SST3/scripts/` — 37 family-prefixed + 3 cross-cutting. Family-prefixed: Phase A (code, 20): `sst3-code-{status,update,search,callers,callers-transitive,callees,subclasses,impact,large,review,config,coverage,orphans,entry-points,untested-py,secrets,cross-lang,shell,recent-changes,at-ref}.sh`. Phase A-security (4): `sst3-sec-{subprocess,deserialize,secret-touchpoints,input-sources}.sh`. Phase A-dep (4): `sst3-dep-{list,usage,blast-radius,cve}.sh`. Phase B (doc, 5): `sst3-doc-{lint,links,yaml,frontmatter,toc}.sh`. Phase C (sync, 4): `sst3-sync-{related-code,tool-eviction,doc-to-code,url-liveness}.sh`. Cross-cutting (3): `sst3-check.sh` (Phase D Layer-2 orchestrator, also exposes the `/sync-check` skill) + `sst3-self-test.sh` (wrapper-lane regression gate) + `sst3-bash-utils.sh` (shared self-bootstrap helper). Inner engines: `ast-grep` + `ripgrep` + `git` + `coverage.py` + `jq` + `markdownlint-cli2` + `lychee` + `yamllint` + `shellcheck` + `python3` + `pip-audit` + `cargo audit` + `npm audit`. See `docs/guides/code-query-playbook.md` for the operational guide.
- **Guide**: `../dotfiles/docs/guides/mcp-configuration.md`
- **Tool Selection**: See `../dotfiles/SST3/reference/tool-selection-guide.md`

### MCP Tools
- **Checkboxes**: `mcp__github-checkbox__update_issue_checkbox(issue_number, checkbox_text, evidence)`
- **Frontend**: Chrome DevTools MCP — guide `../dotfiles/docs/guides/chrome-devtools-mcp.md`, screenshots → `../screenshots/`
- **GitHub Issues**: issue_write, add_issue_comment, search_issues, get_file_contents, create_pull_request

### Google Drive Sync Conflicts
Edit fails with "File has been unexpectedly modified" → copy to `C:/temp/`, edit copy, copy back. See `docs/guides/google-drive-sync.md`.

---
<!-- ============================================================== -->
<!-- ⚠️ DO NOT MODIFY OR DELETE ANYTHING ABOVE THIS LINE ⚠️ -->
<!-- ============================================================== -->
<!-- All content ABOVE is SST3 standard managed by dotfiles issues -->
<!-- Modifications require dotfiles repository SST3 issue approval -->
<!-- Project-specific configuration begins BELOW this boundary -->
<!-- ============================================================== -->



























# Project-Specific Configuration

## Project Overview

MCP server for managing eBay listings from Claude Code. Wraps eBay's Trading API to enable listing creation, bulk description updates, photo uploads, and inventory management directly from the terminal.

## Technology Stack

- Language: Python 3.11+
- Framework: FastMCP (MCP server SDK)
- Dependencies: ebaysdk (Trading API), httpx, Jinja2, Pillow, python-dotenv
- Package manager: uv

## Repository Structure
```
ebay-seller-tool/
├── server.py              # MCP server entrypoint (FastMCP)
├── ebay/                  # eBay API client layer
│   ├── client.py          # Trading API connection factory
│   ├── listing.py         # Create/revise/end listing logic
│   ├── inventory.py       # Quantity management, bulk ops
│   ├── photos.py          # Photo upload and processing
│   └── conditions.py      # Condition name to eBay ID mapping
├── business/              # Business rules (private, loaded at runtime)
│   ├── title_generator.py # Title builder with configurable rules
│   ├── warning_rules.py   # Compatibility warning engine
│   └── part_lookup.py     # Part number lookup integration
├── templates/             # Jinja2 HTML templates
│   ├── base.html          # Base listing HTML shell
│   └── warnings/          # Warning block templates
├── scripts/               # Standalone utilities
│   ├── auth_setup.py      # OAuth2 / Auth'N'Auth initial setup
│   └── export_listings.py # Dump active listings to JSON
├── docs/
│   └── research/          # Decision logs and API research
├── pyproject.toml
└── CLAUDE.md              # This file
```

## Development Setup
```bash
# Clone and setup
git clone https://github.com/hoiung/ebay-seller-tool.git
cd ebay-seller-tool
cp .env.example .env
# Fill in eBay credentials in .env

# Install dependencies
uv sync

# Test with MCP Inspector
uv run mcp dev server.py
```

## Project Standards

### Code Quality
- Linter: ruff
- Formatter: ruff format
- Type checking: mypy (planned)

### Testing
```bash
# Run all tests
uv run pytest -x --tb=line -q

# Test MCP server interactively
uv run mcp dev server.py
```

### Git Workflow
- Branch naming: `solo/issue-[number]-description`
- Commit format: `type: description (#issue)`

## Common Commands
```bash
# Run MCP server (for Claude Code registration)
uv run python server.py

# Register with Claude Code
claude mcp add ebay-seller-tool -- uv --directory /path/to/ebay-seller-tool run python server.py

# MCP Inspector (browser-based tool tester)
uv run mcp dev server.py
```

## Project-Specific Notes

- **eBay marketplace**: Configured via .env (site ID, currency, marketplace ID).
- **Trading API**: Used for listing CRUD (supports CDATA HTML descriptions). Auth'N'Auth token (18-month lifetime).
- **REST Inventory API**: Used for quantity management and bulk price updates.
- **Credentials**: NEVER commit .env. Use .env.example as reference.

## API Quota Tracking (Trading + Sell Analytics)

`ebay/call_accountant.py` is the daily call accountant for both Trading API
verbs (5000/day flat cap) and REST Sell Analytics (`sell_analytics` namespace,
9600/day ≈ 400/hr standard-seller tier — added in #21 Phase 1).

- Trading verbs go through `record_call(verb)` automatically via
  `execute_with_retry` in `client.py`.
- Sell Analytics goes through `account_call(api_namespace="sell_analytics")`
  inside `fetch_traffic_report_raw` (which `fetch_traffic_report` delegates
  to — see `ebay/rest.py`). Pre-flight quota gate + record. Raises
  `RateLimitError` before any network round-trip if quota would be exceeded;
  the error names `api_namespace + remaining + cap` so the operator sees
  exactly which surface tripped.
- Counters are namespace-isolated: a Trading-API spike does NOT eat into
  Sell Analytics quota and vice versa.

### Sell Analytics surface (Issue #31)

`ebay/rest.py` exposes two public surfaces for Sell Analytics traffic:
- `fetch_traffic_report(listing_ids, days=30)` — **parsed-by-default**.
  Returns the decoded shape (impressions, views, transactions, ctr_pct,
  sales_conversion_rate_pct) plus abbreviated demo-style aliases (`imp`,
  `tx_count`, `conv_pct`) plus `per_listing_summary` keyed by listing_id
  in the ebay-ops Fetchers Protocol shape (`{imp, views, ctr_pct,
  conv_pct, tx_count}`). Most callers should use this.
- `fetch_traffic_report_raw(listing_ids, days=30)` — returns the raw eBay
  JSON wire shape (`header.metrics` + `records[*].metricValues`). For
  tests and advanced callers that need to assert on eBay's wire format.

`per_listing_summary` is canonical: `parse_traffic_report_response` pre-
aggregates (listing × day) records into per-listing dicts in abbreviated
keys, so consumers (e.g. `ebay-ops/scripts/run_weekly_tune_live.py`) read
the Fetchers Protocol shape directly without hand-translation. Empty values
are `0.0` (NOT `None`) to match the demo Fetchers contract — `float(None)`
TypeErrors at the orchestrator's `float(traffic.get("ctr_pct", 0.0))` call
were the regression that surfaced during Stage 5 audit.

### Sell Analytics burst-rate-limit (Issue #31)

eBay enforces an undocumented per-window burst rate-limit separately from
the daily quota. `fetch_traffic_report` retries 429s internally with
exponential backoff (5s → 15s → 60s, 80s wall-clock budget — see
`_BURST_RETRY_BACKOFF_SECONDS` + `_BURST_RETRY_TOTAL_BUDGET_SECONDS` in
`rest.py`). After the budget is exhausted it raises
`TrafficReportRateLimitError(attempts, total_wait_seconds, last_error)`
so the caller can degrade gracefully or fail loud — distinct from
`call_accountant.RateLimitError` (daily quota; fires before any network
round-trip).

In addition to the daily counter, `call_accountant.BURST_WINDOWS["sell_analytics"]
= (30, 10)` configures an in-process rolling-window tracker that logs a
warning when 10+ calls fire in 30 seconds — operator-visible only, never
gates. Window threshold is empirical (#31 first-live-run observation:
~22 calls in ~22s tripped 429 with a >5min cooldown).

## Skill Integration

**Skill command**: `/ebay-seller-tool` (private, at `~/.claude/skills/ebay-seller-tool/SKILL.md`)
**Renamed from**: `/ebay-listing` (2026-04-10)

The skill contains all private business rules (title formulas, warning templates, condition descriptions, part number placement). The MCP server provides the API transport layer. When MCP tools are available, the skill calls them directly. When not available, the skill falls back to creating HTML files for manual copy-paste.

**Workflow**:
1. User invokes `/ebay-seller-tool` with drive details
2. Skill loads business rules from private dotfiles research docs
3. Skill calls MCP tools (if registered) or creates HTML files (fallback)
4. Business rules are the SAME regardless of delivery method

## Public / Private Data Split

This repo is **PUBLIC**. Business-sensitive data lives in the **PRIVATE** `dotfiles` repo and is referenced by path. Never copy private data into this repo.

### PUBLIC (this repo)
- MCP server code (server.py, ebay/, business/)
- Generic eBay API integration logic
- Jinja2 template engine (code, not content)
- API research doc (`docs/research/`)
- README, CLAUDE.md, pyproject.toml

### PRIVATE (dotfiles repo — never copy here)
- Business rules, listing strategies, and research docs
- Listing skill with product-specific rules
- Listing HTML files and product photos
- Any data that identifies the seller, products, or business strategies

### What NEVER goes in this repo
- Listing content with real product data
- Business strategies or pricing decisions
- Product-specific lookup results or mappings
- Inventory details, stock levels, product categories
- Customer data, order details, serial numbers
- eBay credentials (.env already gitignored)
- Store username or eBay profile URL
- File paths that reveal business folder structure
- **Secret-scan alert logs** — when `issue-body-scan.yml` or commit-message-scan CI fires, log the event in the PRIVATE channel at `~/.claude/projects/-home-hoiung-DevProjects/memory/secret_scan_leak_log.md`. NEVER create a public GitHub issue for leak tracking — a `[Private]` prefix does NOT make an issue private. See `feedback_public_artefact_leaks_in_issues.md` for the rule.

## Documentation Links
- Project README: `README.md`
- API Research: `docs/research/`
- Skill (private): `~/.claude/skills/ebay-seller-tool/SKILL.md`
  - **Pricing Review Workflow** subsection (#13 — live 2026-04-25): full sweep procedure + 4-phase new-listing flow + verdict→action mapping. Cadence: weekly via skill prompt when `pricing_review.md` `last_full_review` is >7 days.
- Business rules (private): `../ebay-ops/docs/research/ebay/` (16 docs)
- Business memory (private): `~/.claude/projects/-home-hoiung-DevProjects/memory/user_ebay_business.md`
- Pricing review state (private): `~/.claude/projects/-home-hoiung-DevProjects/memory/pricing_review.md`
- Pricing elasticity log (out-of-repo): `~/.local/share/ebay-seller-tool/price_snapshots.jsonl`
- **Default-shipping policy POISONED (durable rule)**: `~/.claude/projects/-home-hoiung-DevProjects/memory/feedback_ebay_default_shipping_poisoned.md` — `_build_seller_profiles_block(include_shipping=False)` for both Add (since #29 revert at `6072a81`) and Revise (since hoiung/ebay-ops#21 Phase 0). Only `scripts/apply_returns_policy.py` uses the default `include_shipping=True` (one-shot Business Policies enrolment migration).

### Comp-filter quality config (Issue #14 + #444 Part B)
- `config/pricing_and_content.yaml` `comp_filter:` block — Layer-1 binary thresholds + Layer-2 deductions + 4 hard-reject regex categories (broken/external/wrong_category/bundle) + caddy_mismatch_patterns + condition_equivalence (numeric Phase 2.3 classes) + series_names (Seagate HARD CONTRACT et al)
- `condition_equivalence` is read by BOTH `score_apple_to_apple` Dim 3 (existing) AND `_sync_find_competitor_prices` orchestrator (#444 Part B): single source of truth, two consumers. Orchestrator dispatches one Browse API call per equivalence-class member and dedupes by `item_id` before reaching the pipeline.
- `config/fees.yaml` `outlier_rejection:` block — Issue #14 Phase 4 IQR-based price-outlier knobs
- Code: `ebay/browse.py` — `_fetch_one_condition_id` (#444), `_sync_find_competitor_prices` (#444 orchestrator), `filter_low_quality_competitors`, `score_apple_to_apple`, `drop_price_outliers`, `run_comp_filter_pipeline` aggregator
- Tests: `tests/test_comp_filter.py` (46 tests, Issue #14 phases + Stage 5 regressions) + `tests/test_browse.py` (24 tests including 10 new #444 equivalence-class tests)
- Diagnostic: `scripts/measure_comp_quality_distribution.py` — calibrate Layer-1 thresholds against a live sweep cache; `scripts/sample_invocation_issue444.py` — AP #18 live sample invocation for the equivalence-class loop

---

*Template Version: SST3.0.0*
*Last Updated: 2026-04-10*
