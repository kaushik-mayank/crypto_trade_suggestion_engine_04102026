# Azure network security group — what to allow

**Inbound: SSH only, and only from your own address.**

| Priority | Name | Port | Source | Action |
|---|---|---|---|---|
| 100 | `allow-ssh-from-me` | 22 | **your public IP /32** | Allow |
| 65500 | `DenyAllInBound` (default) | any | any | Deny |

That is the whole list. In particular:

- **Port 8000 is never opened.** The API binds `127.0.0.1` and `cloudflared` reaches it from inside
  the machine. Opening 8000 would make the token the only thing between the internet and the API;
  not opening it means there is nothing to reach in the first place.
- **No 80, no 443.** Nothing on this VM serves the public. The dashboard is on Vercel.
- **Source `*` on the SSH rule is the mistake to avoid.** Azure's portal offers it as a default and
  it means every SSH scanner on the internet can reach your login prompt. Use your own IP; if it
  changes, edit the rule — that is a 20-second job and it is the right trade.

**Outbound: leave the default (allow all).** The VM has to reach `api.coindcx.com`,
`public.coindcx.com`, Cloudflare for the tunnel, and the Ubuntu package mirrors.

## Checking it afterwards

From your laptop, with the VM running:

```bash
# Should hang and then time out. If it connects, port 8000 is open and must be closed.
curl --max-time 5 http://YOUR_VM_IP:8000/api/health

# Should connect.
ssh crypto@YOUR_VM_IP
```

A `curl` that *succeeds* there is the single most important thing to catch on this page.
