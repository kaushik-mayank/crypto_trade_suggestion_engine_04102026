# 18 — Deployment & Security Hardening

**Depends on:** `specs/10` (two processes, localhost binding), `specs/14` (bind guard),
`specs/17` (recorder heartbeat as the loudest alert).

Two separate problems, and they have different urgency:

1. **Making the repository safe to push to GitHub** — needed now, before the first push.
2. **Hosting** — deferred since Phase 0. This is the point at which it must be decided.

---

## 1. Pre-push checklist — complete before `git push`, every item

Run this once before the first push and again before any push that touches configuration.

- [ ] `.gitignore` contains `.env`, `data/`, `*.db`, `*.db-wal`, `*.db-shm`, `*.parquet`,
      `__pycache__/`, `.venv/`
- [ ] `.env.example` exists with **placeholder values only** — no real key, no real equity figure
- [ ] **Scan the full history, not just the working tree:**
      `git log -p --all | grep -iE '(api[-_]?key|secret|x-auth)'` returns nothing real
- [ ] No hardcoded secret anywhere: `grep -rn` across `src/`, `tests/`, `specs/`, notebooks
- [ ] `data/` is not tracked — it contains the holdout (`specs/09` §1.2), which must never reach
      a shared repository where it could be inspected before Stage 10
- [ ] `validation/protocol.lock.json` **is** committed — `specs/09` requires git history to prove
      it predates any backtest output
- [ ] No local equity configuration committed (`specs/08` §2.3 keeps it local)
- [ ] Test fixtures contain only public market data — captured exchange payloads are public and
      fine; anything containing an auth header is not

> **If a secret was ever committed, rotating it is mandatory and non-negotiable.** Rewriting
> history does not help — the value is already in every clone and in GitHub's reflog. Rotate
> first, clean history second, in that order.

---

## 2. Hosting — the decision

### Requirements

| # | Requirement | Source |
|---|---|---|
| 1 | **The recorder runs 24/7** | `specs/17` §9 — the only failure that costs data permanently |
| 2 | The app runs 24/7 | Signal generation |
| 3 | Dashboard reachable by one person | `specs/15` |
| 4 | Low cost | Personal project |
| 5 | Low operational overhead | One developer, no ops |
| 6 | Tiny data volume | ~260k rows and <100 MB per month (`specs/11`) |

Requirement 1 is what rules out the laptop: it sleeps, and every sleeping minute is an
unrecoverable gap in funding and positioning history.

### Options

| Option | Cost | 24/7 | Verdict |
|---|---|---|---|
| Laptop | £0 | ✗ | **Fails requirement 1** |
| Raspberry Pi at home | ~£60 once | ✓ | Viable; exposed to home power and connectivity |
| **Small VPS (1–2 GB)** | **~$5/mo** | ✓ | **Recommended** |
| Managed container platform | $$ | ✓ | Overkill; more surface, more to learn, no benefit at this size |
| Serverless | — | ✗ | Wrong shape — this is a long-running stateful process |

### Recommendation

> **A 1–2 GB VPS in Mumbai or Singapore.**

Region is not arbitrary. Round-trip latency has a **measurable** effect: `BAS_BPS` discards any
spot/futures pair whose timestamps differ by more than 2 seconds (`specs/02` Family 6), and
`DQ_LATENCY` measures TPE relay time directly. A closer region raises the usable basis sample
rate. At an hourly horizon latency is otherwise irrelevant — this is the one place it shows up.

### Runtime: systemd + a pinned virtualenv, not Docker

Phase 10 provided a Dockerfile but did not require it. On a dedicated VPS running exactly two
Python processes, Docker adds a layer between you and every problem for a reproducibility benefit
that has no second environment to deliver into.

`specs/10` §1 ranks **debuggability** fourth overall and above scalability. Keep the Dockerfile
for portability; deploy with systemd and a locked venv.

---

## 3. Network security — do not expose the dashboard

`specs/14` §11 binds `127.0.0.1` by default and refuses a non-localhost bind without an auth
token. On a VPS you must reach it somehow. Three ways:

| Approach | Assessment |
|---|---|
| Public port + auth token + TLS | Works. Puts a page displaying trading positions on the public internet, permanently, for one user |
| Reverse proxy with auth | Same exposure, more moving parts |
| **Private network (Tailscale / WireGuard) or SSH tunnel** | **Recommended.** The dashboard never touches the public internet |

> **Recommended: keep the app bound to localhost and reach it over a private network or an SSH
> tunnel. Never open the port.**

This eliminates an entire attack surface rather than defending it, and it satisfies `specs/14`'s
bind guard without weakening it — the app still binds localhost; the private network provides the
path. For one user this is both the simplest option and the strongest.

Firewall: inbound SSH only (key auth, password login disabled). Everything else denied.

---

## 4. Secrets on the host

One secret, used by one module (`specs/10` §9).

```
/opt/suggester/.env      chmod 600, owned by the service user, not root
```

Loaded by systemd `EnvironmentFile=`. **No secrets manager** — Vault or a cloud KMS is operational
overhead for a single low-value credential on a single machine, and adds a dependency whose
failure takes the system down.

### The payoff of the least-privilege work, stated plainly

The key has **no trading permission and no withdrawal permission** (`specs/00` §10), and the
system never reads a balance (`specs/08` §2.3). So the blast radius of a leaked key is:

> Someone could read this account's `data/stats` and `conversions` — public-equivalent market
> statistics. **Nothing else.** No orders, no withdrawals, no balance visibility.

That is the entire return on the Phase 0 finding that only two endpoints need credentials. It is
worth stating because it changes how much anxiety this deployment deserves: a leak here is an
inconvenience, not an incident.

### Rotation

Routine, and cheap by design: update `.env`, `systemctl restart`. Because exactly one module reads
the secret, nothing else changes. **Rotate on any suspicion and on a schedule.** Drill it (§7).

---

## 5. Process supervision

Two systemd units mirroring the two processes (`specs/10` §3):

```
suggester-recorder.service    Restart=always   RestartSec=10
suggester-app.service         Restart=always   RestartSec=10
```

> **The units must be independent.** No `Requires=`, no `BindsTo=` between them.

If the app crash-loops, the recorder must keep recording. Coupling the units would reintroduce at
the process-supervision layer exactly the single point of failure that `specs/11` §1 refused to
create at the data layer.

Both `WantedBy=multi-user.target` so they start on boot. `StartLimitIntervalSec=0` on the recorder
— systemd's default crash-loop protection would give up and stop restarting it, which is the wrong
behaviour for the one process whose downtime is permanent.

That last detail is easy to miss and turns a transient fault into indefinite data loss.

---

## 6. Backups

> **`data/raw/` is the only data in this system that cannot be reconstructed.**

Everything else can be rebuilt: bars refetch from the API, features recompute, signals replay
(`specs/13` §10), Parquet regenerates. The recorder's JSONL archive holds funding, predicted
funding, mark price and positioning history that **no endpoint will ever serve again**.

| Path | Backup | Reason |
|---|---|---|
| `data/raw/` | **Nightly, off-host** | Irreplaceable |
| `data/state.db` | Nightly | Replaceable but expensive — signal history and audit trail |
| `data/holdout/` | Nightly, **separate destination** | Must not be casually browsable (`specs/09` §1.2) |
| `data/parquet/` | Never | Derived |
| `data/bars` in SQLite | Covered by `state.db` | Refetchable anyway |

Method: nightly `rclone`/`rsync` to cheap object storage or the user's machine. **Test the
restore** (§7) — an untested backup is a hypothesis.

---

## 7. Recovery drills

Run each once before declaring deployment complete. Record the result.

| # | Drill | Pass condition |
|---|---|---|
| 1 | `kill -9` the app mid-evaluation | Restarts; setups re-arm with original `armed_at_ms`; no duplicate signals (`specs/13` §11.1) |
| 2 | `kill -9` the recorder mid-write | Restarts; JSONL intact; gap recorded in `_meta/gaps.jsonl` |
| 3 | Reboot the host | Both units start; gaps detected and candle data backfilled |
| 4 | Block the API hosts for 30 min | Backoff behaves, no hot loop, recovery automatic, gap recorded, `DOWN` shown in the UI |
| 5 | Fill the disk | Writes fail gracefully, alert fires, no corruption |
| 6 | Restore `data/raw/` from backup | Archive is complete and parseable |
| 7 | **Rotate the API key** | Only `.env` changes; one restart; nothing else touched — proves the least-privilege boundary is real |
| 8 | Kill the recorder and leave it down | The heartbeat alert fires within one cycle and is the loudest signal in the UI (`specs/17` §9) |

Drill 7 is as much an architecture test as a security test. If rotation requires touching more
than `.env`, the secret has leaked into somewhere it should not be.

---

## 8. Time synchronisation

Enable NTP explicitly (`systemd-timesyncd` or `chrony`) and verify.

Not housekeeping. A drifting clock silently degrades three things: `DQ_AGE` freshness gating
(`specs/11` §9), the 2-second basis tolerance that discards misaligned samples, and — if the clock
drifts far enough — signed requests, since the futures order docs mention a 10-second rejection
window. Probe P15 measures skew; NTP is what keeps it small.

---

## 9. Reproducibility

- Pin the Python version (`.python-version` or `pyproject.toml` `requires-python`).
- Commit the lockfile (`uv.lock` or hash-pinned `requirements.txt`). Deploy installs from the
  lock, never resolves fresh.
- One documented deploy path: pull, install from lock, run migrations, restart units.
- **Every signal already records `code_version` and `parameter_hash`** (`specs/10` §9) — so a bad
  deploy is attributable rather than mysterious, and `replay_current` (`specs/13` §10) shows
  exactly what it changed.

---

## 10. Alert delivery when nobody is watching

On a VPS the dashboard is often closed, and the one alert that must not wait is the recorder
heartbeat.

`specs/17` §9 keeps the outbound webhook optional and disabled by default. **For a hosted
deployment, enable it for the recorder-heartbeat alert specifically.**

The honest trade: one more secret (a bot token), with a low blast radius, in exchange for knowing
within minutes when permanent data loss begins rather than at the next dashboard visit. Worth it
for that one alert. Not worth it for the rest, which can wait.

---

## 11. What not to build

- **No CI/CD pipeline to production.** One developer, one machine, manual deploy. An auto-deploy
  on push is a mechanism for shipping an untested change to a live system at 2am.
- **No secrets manager** (§4).
- **No container orchestration** — two processes.
- **No public exposure** (§3).
- **No monitoring SaaS.** The health endpoint, status panel and one webhook are sufficient for one
  user. Revisit only if a second person ever depends on it.
- **No auto-scaling, no load balancer, no CDN.**

Each is reasonable at a different scale. None is reasonable here, and each would add a failure
domain and a surface to secure.

---

## 12. Parameters — zero

Running total: **unchanged (29 or 30 after `specs/16` Stage 3).** Host configuration is not model
configuration.

---

## 13. Acceptance criteria for Phase 18

1. Every pre-push checklist item in §1 verified, **including the full git-history scan**.
2. Any previously committed secret rotated before history cleanup.
3. Deployed to a 24/7 host; the recorder runs continuously and independently.
4. systemd units are **independent**, both start on boot, and the recorder has crash-loop
   protection disabled.
5. **The dashboard port is not open to the internet**; access is via private network or SSH tunnel.
6. Firewall allows inbound SSH only; password login disabled.
7. `.env` is `0600`, owned by the service user, loaded via `EnvironmentFile`.
8. NTP enabled and clock skew verified against probe P15's tolerance.
9. All eight recovery drills executed and recorded — including a real key rotation and a real
   backup restore.
10. Nightly backup of `data/raw/` and `state.db` running, with a **tested** restore.
11. Lockfile committed; deploy installs from it.
12. Recorder-heartbeat alert reaches the user when the dashboard is closed.
