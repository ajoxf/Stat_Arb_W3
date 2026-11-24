# Build the Nexus Client Portal on Vercel + Supabase

Paste everything below into a new chat. It is self-contained.

---

## Who I am and what exists

I run **Nexus**, a spread-trading desk for OKX USDT perpetual swaps (ETH vs BTC, SOL vs BTC). It lives in the GitHub repo `ajoxf/Stat_Arb_W3`, folder `okxtrader/`, on branch `claude/crypto-arbitrage-system-RN3Jr-c2Lx8`. It is a Python/Flask desk that runs on my own Windows PC (`python start.py`).

The desk's **Algo** trades my own account. A **mirror** add-on (`okxtrader/mirror.py`) copies every Algo trade into my clients' accounts. Each client account is an **OKX Managed Trading Sub-account** bound to my desk and traded with its own API key. Each client takes a **multiple** of my trade size.

As **fund manager**, I take a **Profit payout**: a percentage, set per client in the desk's **Managed accounts** screen, of each month's profit. A losing month pays nothing, and there is **no high-water mark**. I mark each month's payout **paid** once it has been paid.

Today the desk has a **local** client dashboard:
- `okxtrader/clientdash.py` (the page);
- `okxtrader/clientbook.py` (the accounting);
- `okxtrader/client_portal.py` plus `run_client_portal.py` (a separate read-only Flask server with its own logins).

It only runs on my PC. **Read these files first.** They define exactly what the client sees and how every number is worked out.

## What I want

A **real, hosted client portal**: Next.js on **Vercel**, with data and logins in **Supabase**. Each client can log in from anywhere and see **only their own account**:

1. **Your account, as OKX holds it:**
   - current balance (OKX total equity);
   - **available margin**;
   - margin in use;
   - open P&L;
   - "as of" time.
2. **Performance:**
   - net deposited (in / out);
   - total P&L after trading fees;
   - return since start;
   - this month (P&L and %);
   - **Profit payout** (with the %, and the paid / due / accruing split);
   - their net P&L after the profit payout.
3. **Monthly returns:** a bar chart of monthly P&L, and a table with month, opening, deposits, withdrawals, P&L, return %, closing, and a "since start" total.
4. **Profit payout:** month, P&L, profit payout %, profit payout, your net, and status (**accruing** for the current month, **due** for an ended month with a payout, **paid** with its date, **none** for a month with no payout), plus totals.
5. **Deposits & withdrawals:** every one, from the sub-account's **own OKX funding history**: date, type (deposit, withdrawal, transfer in from the main account, transfer out to the main account), amount, and source "OKX".

**Never shown to a client:** anything about what was traded, meaning instruments, sides, sizes, individual trades, number of trades, or ladders.

## Architecture

```
Desk PC (Python, existing)                      Cloud
─────────────────────────                       ─────────────────────────────
okxtrader engine + mirror  ──push (HTTPS)──▶    Supabase (Postgres + Auth + RLS)
  reads OKX: balances, funding history,                ▲
  closed copies' P&L, payouts marked paid              │ client reads its own rows
                                                Next.js app on Vercel  ◀── clients log in
```

- **The desk pushes. Nothing pulls from the desk.** The desk stays on my PC and is never exposed to the internet. The portal cannot reach the desk and cannot trade.
- **A new desk module, `okxtrader/portal_sync.py`,** pushes each client's data to Supabase:
  - every few minutes, and right after a payout is marked paid or unmarked;
  - using the Supabase **service-role key**, which lives **only in the desk's `.env`** (never in the browser, never in git, never in Vercel's public env);
  - with idempotent upserts keyed on natural ids (OKX bill id, desk position id, `(client_id, month)`).

  It must be:
  - **off unless configured**;
  - **never fatal** to the desk (it runs on its own thread, and a failed push is logged and retried);
  - the **only** change to the desk, apart from a small hook and a "Portal sync" status line on the Mirror tab or in Managed accounts.
- **Choose one and say which:**
  - **(a)** Push the raw rows (flows, closed copies' month / P&L, balances, payouts) and work out the monthly table in the portal. If so, port `clientbook.book()` to TypeScript **exactly**, with the same tests.
  - **(b)** Push the computed book (summary plus months) from the desk.

  I lean to **(b)**: one source of truth for the arithmetic, already tested in `tests/test_clientbook.py`. Raw flows are still pushed for the history table.

## Supabase

My Supabase project is already connected to Claude Code: `ifmpyoebramjefhpxqjl.supabase.co`. Use the Supabase tools (migrations, SQL, advisors) rather than asking me to click.

Tables (adjust names if you have a reason, and say why):

- **`clients`:** `id uuid pk`, `account_name text unique` (the desk's follower name), `display_name`, `profit_payout_pct numeric`, `currency text default 'USD'`, `created_at`, `active bool`.
- **`client_users`:** `user_id uuid` (references `auth.users`), `client_id` (references `clients`), `role text check (role in ('client','manager'))`. A client user maps to exactly one client. A manager sees all clients.
- **`balances`:** `client_id`, `equity`, `available`, `margin_used`, `unrealized`, `as_of timestamptz`. Keep a history; the latest row is "current".
- **`flows`:** `client_id`, `okx_bill_id text`, `at`, `amount` (+ in, − out), `kind`, unique `(client_id, okx_bill_id)`.
- **`months`:** `client_id`, `month date`, `opening`, `deposits`, `withdrawals`, `pnl`, `return_pct`, `payout`, `client_net`, `closing`, `status` (accruing / due / paid / none), `paid_at`, `paid_reference` (**manager-only**; see below), unique `(client_id, month)`.
- **`summary`:** one row per client with the `clientbook` summary fields, `updated_at`.
- **`sync_log`:** what the desk pushed and when, so I can see the sync is alive.

**Row-level security on every table.**
- A **client** can `select` only rows whose `client_id` is theirs (through `client_users`). They can never insert, update or delete anything.
- A **manager** can read everything.
- **Writes come only from the desk's service-role key.**
- `paid_reference` must not be readable by clients. Use a view or column-level grants.

Run Supabase's security advisors and fix every finding. Write the policies as migrations in the repo, under `portal/supabase/migrations/`.

**Auth:** Supabase Auth with email + password.
- **Clients don't sign up themselves.** I invite them: the desk's Managed accounts screen gets an "Invite to portal" action (email), done through the sync module's admin call, or a manager page in the portal.
- Password reset by email.
- Optional TOTP MFA, which I'd like offered.
- Sessions use the Supabase SSR helpers for Next.js.

## Vercel

The Vercel MCP tools are connected to Claude Code. Use them to create the project, set environment variables and deploy.

- **App:** Next.js (App Router, TypeScript) in `portal/` in the same repo, deployed from my branch. If a separate Vercel project root is needed, say so.
- **Environment variables on Vercel:** only `NEXT_PUBLIC_SUPABASE_URL` and `NEXT_PUBLIC_SUPABASE_ANON_KEY`. **The service-role key is never on Vercel.**
- **Pages:**
  - `/login`
  - `/reset-password`
  - `/` (dashboard: the client's own; for a manager, a client picker)
  - `/account` (change password, MFA)
- **Design:** clean and professional; reproduce the local dashboard's layout and wording. I'll attach screenshots of it; its exact HTML and CSS are in `okxtrader/clientdash.py`. Server-rendered, works on a phone (two tiles per row), prints well, light theme. No external fonts or scripts beyond what Next.js needs.
- **HTTPS only,** with security headers (CSP, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`) and no indexing (`robots: noindex`).
- **A custom domain later.** Give me the steps; don't buy anything.

## Security rules (non-negotiable)

- A client must **never** see another client's data. Prove it with tests: two clients and one manager against a real Supabase (local CLI or a branch), checking every table and view through RLS.
- No trading capability anywhere in the portal. The portal never holds an OKX key.
- No client data, keys or logins in git. Check `.gitignore`.
- Wrong password and unknown email get the same error. Rate-limit logins (Supabase's settings).
- Note anything I need to decide or do by hand: email provider (SMTP) for invites, domain, and VARA licensing (managing third-party money in Dubai is a licensed activity; see below).

## How I work (please follow)

- Push only to branch `claude/crypto-arbitrage-system-RN3Jr-c2Lx8`. **No pull requests unless I ask.**
- **Never commit with failing tests.** The desk has about 1,680 pytest tests (`python -m pytest -q`); add tests for every change. The portal gets its own tests (unit tests for any arithmetic, RLS tests, and a Playwright smoke test of login and dashboard).
- `okxtrader/static/ladder.css` must stay byte-identical (a test pins its hash).
- Credentials only in `.env`, never in chat or git.
- Commit messages end with the attribution lines; no model names in code or commits.
- Analyse first, and **ask before changing anything that affects trading**. The portal work must not touch execution.
- Show me screenshots of the portal before you call it done: login, dashboard on desktop and phone, manager view.

## Context: what is already true in the desk

- **Each follower's data is in `mirror_state.json`:**
  - `cash[name].balance`: `equity`, `available`, `margin_used`, `unrealized`, `at`;
  - `cash[name].flows`: by OKX bill id, from `/api/v5/asset/bills-history` with types 1 / 2 / 13 / 22 / 23;
  - `closed[name]`: each closed copy with `closed_at` and `pnl` after fees.
- **Payouts marked paid** are in `client_payouts.json`: `{account: {'YYYY-MM': {at, amount, note}}}`.
- **Logins** for the local portal are in `client_logins.json` (salted PBKDF2). The hosted portal replaces these with Supabase Auth. Keep the local portal working until I say otherwise.
- **The profit payout % is per follower** in `config.json → mirror.followers[].profit_share_pct`. It's shown as "Profit payout %" and set in the desk's Managed accounts screen.
- **Things that are still unconfirmed:**
  - that a managed sub-account's own key can read its funding history (to be checked on a demo sub-account);
  - VARA: OKX UAE is VARA-licensed, but managing others' virtual assets needs my own licence. I am only building and testing; **no client capital** yet.

## Done means

1. Migrations and RLS policies are applied to my Supabase project, and the advisors are clean.
2. `portal_sync.py` pushes real data from the desk (off by default, configured from Managed accounts), with tests.
3. The Next.js portal is deployed on Vercel. I can invite a test client, they can log in and see only their own dashboard, and a manager login sees all.
4. The RLS isolation tests pass against a real database.
5. The full desk test suite passes, and everything is pushed to the branch.
6. A short runbook (`portal/README.md`) covers: inviting a client, resetting a password, rotating keys, what to do if the sync stops, and the domain and SMTP setup.
