# Hosting guide

**Who this is for:** someone who has never set up a server. Every command is written out. If a step
needs a decision, the decision is explained and a recommendation given.

**Roughly 90 minutes**, most of it waiting for Azure and for the first data fetch.

**What you will end up with:**

```
         your browser
              │
              ▼
      Vercel  (the dashboard — public, free)
              │  server-side, carries the shared token
              ▼
   Cloudflare tunnel  (no open ports)
              │
              ▼
      Azure VM  (the engine — private)
         ├── crypto-ingest   fetches prices, writes the database
         ├── crypto-app      works out suggestions, serves the API
         └── crypto-tunnel   lets Vercel in, without opening a port
```

---

## Read this first — two things that matter

**This system cannot place a trade.** Not "is configured not to" — there is no code anywhere in the
project that places, cancels or transfers anything, and an automated test fails the build if an
order or wallet address ever appears in it. It reads prices and suggests. A person decides.

**Nothing has been validated yet.** The suggestions you will see have never been tested against
history. Hosting this proves the *machinery* works — data stays current, errors are honest, the
dashboard never shows stale numbers as fresh. It does not tell you the suggestions are any good.
Treat everything on the dashboard as something to watch, not something to act on.

---

## What you need before starting

| Thing | Why | Cost |
|---|---|---|
| An Azure account | Runs the engine | ~$20–35/month, or free for 12 months on a new account |
| A GitHub account | You already have this; the code lives there | Free |
| A Vercel account | Hosts the dashboard | Free |
| A Cloudflare account | The tunnel, so no port is ever opened | Free |

You do **not** need: a domain name, a CoinDCX API key (nothing this system reads needs one), Docker,
or any paid tier of anything except the VM.

---

## Step 1 — Create the Azure virtual machine

1. Go to [portal.azure.com](https://portal.azure.com) → search **Virtual machines** → **Create** →
   **Azure virtual machine**.

2. Fill in the **Basics** tab:

   | Field | What to put | Why |
   |---|---|---|
   | Resource group | **Create new** → `crypto-trader` | A folder for everything, so deleting it later deletes everything |
   | Virtual machine name | `crypto-vm` | |
   | Region | **(Asia Pacific) Central India** | Measured as the lowest latency to CoinDCX. Any region works; this one is fastest |
   | Image | **Ubuntu Server 24.04 LTS - x64 Gen2** | |
   | Size | **B2s** (2 vCPU, 4 GiB) | B1s (1 vCPU, 1 GiB) also works and is free for 12 months on a new account, but is slow building the first history |
   | Authentication type | **SSH public key** | A password can be guessed. A key cannot |
   | Username | `azureuser` | |
   | SSH public key source | **Generate new key pair** → name it `crypto-vm-key` | |
   | Public inbound ports | **Allow selected ports** → tick **SSH (22)** only | **Do not tick HTTP or HTTPS.** Nothing on this machine serves the public |

3. Go to the **Disks** tab:
   - OS disk size → **Premium SSD, 64 GiB**.
   - 64 GiB, not the default 30: the price database grows by roughly 2 GB a month, and a full disk
     stops the engine writing while leaving it apparently running.

4. **Review + create** → **Create**.

5. A box pops up: **Download private key and create resource**. Click it. A file called
   `crypto-vm-key.pem` downloads. **You cannot download it again.** Put it somewhere you will not
   lose it.

6. Wait ~2 minutes. Then open the VM's page and copy its **Public IP address**.

### Lock down SSH (2 minutes, worth it)

Azure's default lets the whole internet reach your login prompt. Narrow it to you:

1. On the VM's page → **Networking** → click the rule named `SSH`.
2. Change **Source** from `Any` to **My IP address**.
3. **Save**.

If your home IP changes later, SSH stops working and you come back here and update it. That is a
20-second job, and it is worth it — see [`deploy/azure/nsg-rules.md`](deploy/azure/nsg-rules.md).

---

## Step 2 — Connect to the VM

On **Windows** (PowerShell), on **macOS** or on **Linux** (Terminal):

```bash
# Replace the path and the IP with yours.
# First, fix the key's permissions — SSH refuses a key that others could read.

# macOS / Linux:
chmod 600 ~/Downloads/crypto-vm-key.pem
ssh -i ~/Downloads/crypto-vm-key.pem azureuser@YOUR_VM_IP

# Windows PowerShell:
icacls "$HOME\Downloads\crypto-vm-key.pem" /inheritance:r /grant:r "$($env:USERNAME):(R)"
ssh -i "$HOME\Downloads\crypto-vm-key.pem" azureuser@YOUR_VM_IP
```

It will ask `Are you sure you want to continue connecting?` → type `yes`.

You are now on the server. Everything from here until Step 7 happens in this window.

---

## Step 3 — Install what the engine needs

Paste these one block at a time.

```bash
# System packages.
sudo apt-get update
sudo apt-get install -y python3.12 python3.12-venv python3-pip git curl sqlite3
```

```bash
# A dedicated user with no login shell and no password.
# The engine runs as this user, so a bug in it cannot read your SSH keys or become root.
sudo useradd --system --create-home --shell /usr/sbin/nologin crypto
sudo mkdir -p /opt/crypto-trader /etc/crypto-trader
sudo chown crypto:crypto /opt/crypto-trader
```

---

## Step 4 — Get the code onto the VM

The code comes from GitHub. The VM pulls it; you never copy files by hand.

**If your repository is public**, this is one command:

```bash
sudo -u crypto git clone https://github.com/kaushik-mayank/crypto_trade_suggestion_engine_04102026.git /opt/crypto-trader
```

**If your repository is private**, GitHub needs to recognise the VM. Create a *deploy key* — a key
that can read one repository and nothing else:

```bash
# 1. Make a key on the VM.
sudo -u crypto ssh-keygen -t ed25519 -N "" -f /home/crypto/.ssh/id_ed25519

# 2. Print the PUBLIC half. Copy the whole line.
sudo cat /home/crypto/.ssh/id_ed25519.pub
```

Then in your browser: your repo → **Settings** → **Deploy keys** → **Add deploy key** → paste it →
name it `azure-vm` → leave *Allow write access* **unticked** → **Add key**.

Back on the VM:

```bash
sudo -u crypto ssh-keyscan github.com >> /home/crypto/.ssh/known_hosts
sudo -u crypto git clone git@github.com:kaushik-mayank/crypto_trade_suggestion_engine_04102026.git /opt/crypto-trader
```

Now install the Python dependencies:

```bash
cd /opt/crypto-trader
sudo -u crypto python3.12 -m venv .venv
sudo -u crypto ./.venv/bin/pip install --upgrade pip
sudo -u crypto ./.venv/bin/pip install -e .
sudo -u crypto mkdir -p data logs
```

That last install takes 2–3 minutes.

---

## Step 5 — Configuration and the one secret

```bash
# Generate the token the dashboard will use to talk to the engine. COPY THE OUTPUT —
# you need it twice: here, and in Vercel in Step 7.
/opt/crypto-trader/.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(48))"
```

```bash
# Create the configuration file from the template.
sudo cp /opt/crypto-trader/deploy/azure/env.example /etc/crypto-trader/env
sudo nano /etc/crypto-trader/env
```

In the editor, find this line and paste your token after the `=`:

```
CT_SERVER__AUTH_TOKEN=
```

Leave everything else as it is. In particular leave `COINDCX_API_KEY` and `COINDCX_API_SECRET`
**empty** — nothing this system reads needs them.

Save and exit: `Ctrl+O`, `Enter`, `Ctrl+X`.

```bash
# Lock the file down: the engine can read it, nothing else can, and the engine cannot change it.
sudo chown root:crypto /etc/crypto-trader/env
sudo chmod 640 /etc/crypto-trader/env
```

### Why the token matters even though nothing is "secret"

The engine holds no credentials and can't trade, so a leak costs you nothing directly. The token is
there because an open API is an open invitation: anyone who found it could read it as fast as they
liked and spend your CoinDCX rate budget, which would make *your* data stop updating. The engine
**refuses to start** without a token once you tell it a tunnel is in front of it, which the template
already does.

---

## Step 6 — Build the price history, then start

This is the part that replaces copying a database up from your laptop. The VM fetches everything
itself from CoinDCX.

```bash
cd /opt/crypto-trader

# First: see what is missing. Makes no requests, so it is safe to run any time.
sudo -u crypto ./.venv/bin/python -m bootstrap --check
```

You will see every series listed as `incomplete` with a bar count. That is expected on a fresh
machine.

```bash
# Now fetch it. About 30 seconds.
sudo -u crypto ./.venv/bin/python -m bootstrap --run
```

It prints a summary ending in `remaining incomplete: 0 series, 0 bars`.

**This command is safe to run again, any time.** It looks at the database, works out what is
missing, and fetches only that. If it is interrupted, run it again — there is nothing to reset. If
everything is already there, it says so and stops.

> **Optional, for backtesting later:** `bootstrap --run --profile full` fetches everything CoinDCX
> has (back to 2021). That takes a few hours and about 6 GB, almost all of it minute-by-minute data.
> You do **not** need it to run the system. Skip it for now.

Now start the two services:

```bash
sudo cp /opt/crypto-trader/deploy/azure/crypto-ingest.service /etc/systemd/system/
sudo cp /opt/crypto-trader/deploy/azure/crypto-app.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now crypto-ingest
sudo systemctl enable --now crypto-app

# Give them 20 seconds, then check.
sleep 20
systemctl status crypto-ingest --no-pager | head -5
systemctl status crypto-app --no-pager | head -5
```

Both should say **active (running)**.

```bash
# Prove the API works and that the token is enforced.

# Without a token — should return 401. If it returns data, stop and check Step 5.
curl -s -o /dev/null -w "no token: %{http_code}\n" http://127.0.0.1:8000/api/health

# With the token — should return 200 and a pile of JSON.
curl -s -o /dev/null -w "with token: %{http_code}\n" \
  -H "x-auth-token: PASTE_YOUR_TOKEN_HERE" http://127.0.0.1:8000/api/health
```

You want `no token: 401` and `with token: 200`.

---

## Step 7 — The tunnel, so Vercel can reach the engine

A tunnel means the VM dials *out* to Cloudflare, and traffic comes back down that connection. **No
inbound port is ever opened.** That is why the firewall can stay closed.

1. Go to [one.dash.cloudflare.com](https://one.dash.cloudflare.com) → **Networks** → **Tunnels** →
   **Create a tunnel** → **Cloudflared** → name it `crypto-trader` → **Save**.

2. Cloudflare shows an install command containing a long token. **Copy just the token** (the long
   string after `--token`).

3. On the VM:

```bash
# Install cloudflared.
curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
  -o /tmp/cloudflared
sudo install -m 755 /tmp/cloudflared /usr/local/bin/cloudflared

# Store the tunnel token. THIS ONE IS A REAL SECRET.
sudo cp /opt/crypto-trader/deploy/azure/tunnel-env.example /etc/crypto-trader/tunnel-env
sudo nano /etc/crypto-trader/tunnel-env
# paste the token after CLOUDFLARE_TUNNEL_TOKEN=  then Ctrl+O, Enter, Ctrl+X

sudo chown root:crypto /etc/crypto-trader/tunnel-env
sudo chmod 640 /etc/crypto-trader/tunnel-env

sudo cp /opt/crypto-trader/deploy/azure/crypto-tunnel.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now crypto-tunnel
sleep 10
systemctl status crypto-tunnel --no-pager | head -5
```

4. Back in Cloudflare, on your tunnel → **Public Hostname** tab → **Add a public hostname**:

   | Field | Value |
   |---|---|
   | Subdomain | `crypto-api` |
   | Domain | pick any domain Cloudflare offers you |
   | Service → Type | `HTTP` |
   | Service → URL | `localhost:8000` |

   **Save**. Cloudflare shows you the full address, e.g. `crypto-api.example.com`. Copy it.

5. Check it works from your laptop (not the VM):

```bash
# Should be 401 — reachable, and refusing anyone without the token. That is correct.
curl -s -o /dev/null -w "%{http_code}\n" https://crypto-api.example.com/api/health
```

---

## Step 8 — Deploy the dashboard to Vercel

1. Go to [vercel.com](https://vercel.com) → **Add New** → **Project** → **Import** your GitHub
   repository.

2. On the configuration screen, change **one** thing:

   | Field | Value |
   |---|---|
   | **Root Directory** | `frontend` |

   Vercel will then detect Next.js on its own. Leave the build and output settings alone.

3. Expand **Environment Variables** and add two:

   | Name | Value |
   |---|---|
   | `CT_API_BASE` | `https://crypto-api.example.com` (your tunnel address, no trailing slash) |
   | `CT_API_TOKEN` | the token from Step 5 |

   Neither name starts with `NEXT_PUBLIC_`, and that is deliberate: a `NEXT_PUBLIC_` variable gets
   compiled into the JavaScript your browser downloads, so the token would be readable by anyone who
   opened the page. These two are read only on Vercel's server.

4. **Deploy**. Two minutes later you get a URL like `crypto-trader.vercel.app`.

5. Open it. You should see the dashboard, with a line at the top saying the system never trades.

---

## Step 9 — Check everything, then leave it alone for 72 hours

```bash
# One command that checks the lot.
/opt/crypto-trader/deploy/azure/health-check.sh
```

Every line should say `OK`. Then **stop touching it.**

The point of the 72 hours is to find out what breaks when nobody is watching — which is the only
way to find that out. Two of the five bugs found during development were caused by processes not
running continuously, and neither would have been found on a laptop that sleeps.

### What to look at, once a day

```bash
# Still running?
systemctl is-active crypto-ingest crypto-app crypto-tunnel

# Anything gone wrong in the last day?
journalctl -u crypto-app -u crypto-ingest --since '1 day ago' | grep '"level": *"error"' | tail -20

# Is the price data still current? Should say 0 bars missing.
cd /opt/crypto-trader && sudo -u crypto ./.venv/bin/python -m bootstrap --check | tail -3
```

### What counts as a problem, and what does not

| What you see | Is it a problem? |
|---|---|
| `No suggestions right now` | **No.** A quiet market is the normal state. Most hours have nothing to say |
| `Warming up` for the first few hours | **No.** Some measurements need 60 days of history and the counters restart on a fresh install |
| A few `error` lines mentioning `DQ_STALE` | **No.** A data source was briefly late and the engine declined to guess. That is the design |
| `Cannot tell — the engine did not answer` | **Yes.** Check `systemctl status crypto-app crypto-tunnel` |
| A service showing `inactive` or `failed` | **Yes.** `journalctl -u crypto-app -n 50` will say why |
| Disk above 85% | **Yes.** The database grows ~2 GB/month |

Every error the engine produces names which of four things is wrong: **the code**, **the
configuration**, **this machine**, or **CoinDCX**. That classification is in the log line as
`problem_class`, and it is there so you know whether to read code or restart a service.

---

## Updating after you push new code

You push to GitHub from your laptop as usual. Then, on the VM:

```bash
/opt/crypto-trader/deploy/azure/update.sh
```

It pulls, reinstalls dependencies, runs the startup checks, and restarts the services — and stops
if any step fails rather than leaving half of one version running.

The Vercel dashboard updates itself: it redeploys on every push to `main`, with no action from you.

---

## When something is wrong

### The dashboard says "Cannot tell — the engine did not answer"

Work from the outside in:

```bash
# 1. Is the tunnel up?
systemctl status crypto-tunnel --no-pager | head -5

# 2. Is the app up?
systemctl status crypto-app --no-pager | head -5

# 3. Does the app answer locally? Expect 401.
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/api/health
```

If step 3 answers but the dashboard still cannot see it, the problem is between Cloudflare and
Vercel: check that `CT_API_BASE` in Vercel exactly matches your tunnel address, with `https://` and
no trailing slash.

### The dashboard says the token is wrong (401 on every panel)

The token in Vercel and the token on the VM differ. They must match character for character.

```bash
# On the VM, show the token so you can compare it.
sudo grep AUTH_TOKEN /etc/crypto-trader/env
```

Then Vercel → Project → Settings → Environment Variables → edit `CT_API_TOKEN` → **Redeploy**.
Vercel does not pick up a changed variable until you redeploy.

### The app will not start

```bash
journalctl -u crypto-app -n 50 --no-pager
```

The commonest cause is a missing or malformed `/etc/crypto-trader/env`. The engine refuses to start
with bad configuration rather than running with half of it, and the error names the setting.

### Price data has stopped updating

```bash
systemctl status crypto-ingest --no-pager
journalctl -u crypto-ingest -n 50 --no-pager | tail -20

# Then fill whatever is missing. Always safe to run.
cd /opt/crypto-trader && sudo -u crypto ./.venv/bin/python -m bootstrap --run
```

### Starting over

Deleting the `crypto-trader` resource group in Azure deletes the VM, its disk and its network in one
go. Nothing is kept anywhere else — the code is on GitHub and the price history can be rebuilt with
one command.

---

## Where every setting lives, and which are secret

| Setting | Where | Secret? |
|---|---|---|
| `CT_SERVER__AUTH_TOKEN` | `/etc/crypto-trader/env` on the VM | **Yes** — shared with Vercel |
| `CLOUDFLARE_TUNNEL_TOKEN` | `/etc/crypto-trader/tunnel-env` on the VM | **Yes** — a real credential |
| `CT_API_TOKEN` | Vercel → Settings → Environment Variables | **Yes** — the same value as the first |
| `CT_API_BASE` | Vercel → Settings → Environment Variables | No |
| Everything else (`CT_PATHS__*`, `CT_SERVER__HOST`) | `/etc/crypto-trader/env` | No |
| `COINDCX_API_KEY` / `SECRET` | `/etc/crypto-trader/env`, **left empty** | Would be, if used |

**Nothing secret ever goes in Git.** `.gitignore` already blocks `.env`, the `data/` folder and all
database files. The two files above live only on the VM, outside the repository, owned by root.

---

## The things this guide cannot give you

Worth being straight about:

- **The suggestions are unvalidated.** No strategy has passed a backtest. Each one is sized at a
  reduced multiple and labelled on the dashboard. Watch them; do not follow them.
- **Two of the twelve strategies cannot be tested at all yet.** They read funding and positioning
  data, which has no history — it only exists from the moment this host starts recording. They will
  produce suggestions with no historical evidence whatsoever behind them.
- **The 72-hour run tests the plumbing, not the ideas.** It answers "does this stay up and tell the
  truth", which is worth knowing and has never been established. It does not answer "does this
  make money".
