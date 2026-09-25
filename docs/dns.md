# DNS and certificates

Every environment served through Traefik (`mode: proxy`) needs a **domain**, `DOMAIN` in its
`platform.env`. Every public host name of that environment is one label under it
(`<host>.<DOMAIN>`), and Traefik gets one wildcard certificate, `*.<DOMAIN>`, from Let's Encrypt
with the DNS-01 challenge. So the DNS provider must:

- resolve `*.<DOMAIN>` to the environment's host, and
- let Traefik create TXT records through an API (the `_acme-challenge` record that proves you own
  the domain).

This guide covers two ways to do it:

| | [Cloudflare with your own domain](#option-a-cloudflare-with-your-own-domain) | [DuckDNS, free](#option-b-duckdns-free) |
|---|---|---|
| Cost | The domain: about US$1–12 a year. Cloudflare's DNS is free | Free |
| Host names | `heimdall.example.dev` | `heimdall.yourname.duckdns.org` |
| Environments | Any number: `example.dev`, `hml.example.dev`, ... | One DuckDNS subdomain per environment (5 free) |
| Extras | Optional proxy: hides the host's IP, DDoS protection, caching | None |
| Reliability | Production grade | A free, volunteer-run service. Fine for personal projects and trials |

For anything other people depend on, prefer a domain of your own.

With `DOMAIN=example.dev`, one host serves:

| Host name | What |
|---|---|
| `yggdrasil.<DOMAIN>` | The web console and the status API (`/api/`) |
| `jenkins.<DOMAIN>` | The Jenkins controller, on the host that runs it. GitHub's webhooks go here |
| `grafana.<DOMAIN>` | Grafana |
| `traefik.<DOMAIN>` | The Traefik dashboard (basic auth) |
| `<host>.<DOMAIN>` | Each application with a `host` in `catalog.yaml`, e.g. `heimdall` and `heimdall-api` |

A wildcard record covers them all: adding an application never needs a DNS change.

The examples below use `example.dev` (or `yourname.duckdns.org`) and a VPS at `203.0.113.10`.

## Option A: Cloudflare with your own domain

### A1. Get a domain

Either:

- **Buy it on Cloudflare**: dashboard → *Domain Registration → Register Domains*. Sold at cost (a
  `.com` is about US$10.50 a year) and already on your account: skip A2.
- **Buy it at another registrar** (Porkbun, Namecheap, ...). Some TLDs (`.xyz`, `.site`) cost
  US$1–3 the first year; check the renewal price. Then do A2.

### A2. Move its DNS to Cloudflare

Only for a domain bought elsewhere.

1. Cloudflare dashboard → **Add a domain** → `example.dev` → the **Free** plan.
2. Cloudflare shows two name servers, like `ada.ns.cloudflare.com` and `bob.ns.cloudflare.com`.
3. At the registrar, replace the domain's name servers with those two.
4. Wait until Cloudflare shows the domain as **Active**: minutes, sometimes a few hours.

### A3. Add the DNS records

Your domain → **DNS → Records → Add record**:

| Type | Name | IPv4 address | Proxy status |
|---|---|---|---|
| `A` | `*` | `203.0.113.10` (the production host) | **DNS only** (grey cloud) to start |
| `A` | `*.hml` | The homologation host, if you have one | **DNS only**, always |

One record per environment, named after its `DOMAIN`: `DOMAIN=hml.example.dev` → record `*.hml`.
A private address is fine for an environment only used on a LAN: Let's Encrypt never connects to
the host, it only reads the TXT record.

Start with **DNS only**: nothing sits between you and the host while you set things up. The proxy is
[step A7](#a7-optional-the-cloudflare-proxy).

### A4. Create the API token

Profile icon (top right) → **My Profile → API Tokens → Create Token → Create Custom Token → Get
started** ([direct link](https://dash.cloudflare.com/profile/api-tokens)). Not the token pages of R2,
Workers or Zero Trust: they only offer their own product's permissions.

- **Token name**: e.g. `yggdrasil-acme`.
- **Permissions**, two rows (*+ Add more* for the second):

  | | | |
  |---|---|---|
  | Zone | Zone | Read |
  | Zone | DNS | Edit |

  Pick **Zone** in the first drop-down first: only then does the second list *Zone* and *DNS*. In
  the newer screen, with a search box instead of drop-downs, search for **Zone Read** and
  **DNS Write** instead.
- **Zone Resources**: Include · Specific zone · `example.dev`.
- **Client IP Address Filtering**: optional, the IPs of the hosts that use the token.
- **TTL**: no end date. An expired token makes renewals fail, silently, weeks later.

**Continue to summary → Create Token**, and copy the token: Cloudflare shows it only once.

*Zone Read* is not optional: Traefik looks the zone up by name before writing to it. Without it,
issuing fails with an error about not finding the zone. Never use the Global API Key.

Check the token from the host:

```bash
export CF_DNS_API_TOKEN=<token>
curl -s -H "Authorization: Bearer $CF_DNS_API_TOKEN" https://api.cloudflare.com/client/v4/user/tokens/verify
curl -s -H "Authorization: Bearer $CF_DNS_API_TOKEN" "https://api.cloudflare.com/client/v4/zones?name=example.dev"
```

The first should answer `"status":"active"`, the second list the zone in `result`. An empty
`result` means Zone Read or the zone resource is wrong.

### A5. Configure the host

`/etc/yggdrasil/acme.env`:

```
CF_DNS_API_TOKEN=<token>
```

`/etc/yggdrasil/platform.env`, the DNS part:

```
DOMAIN=example.dev
ACME_EMAIL=you@example.com
ACME_DNS_PROVIDER=cloudflare
ACME_CA_SERVER=https://acme-staging-v02.api.letsencrypt.org/directory
JENKINS_URL=https://jenkins.example.dev/
```

These are only the DNS lines. The platform won't start, and even `platform.sh logs` fails, until
the other required variables are filled in too: see
[the checklist](#2-start-the-platform-against-the-staging-ca). The staging CA is for the first run
only. Each environment host has its own `platform.env` with its
own `DOMAIN` (`hml.example.dev` for homologation). The same token serves every host of the zone.

### A6. Bring it up

Continue with [Bring it up](#bring-it-up-either-option).

### A7. Optional: the Cloudflare proxy

The proxy (orange cloud) hides the host's IP and adds DDoS protection and caching. To turn it on:

1. **SSL/TLS → Overview → Full (strict).** Required: with *Flexible*, Cloudflare speaks HTTP to
   Traefik, Traefik redirects to HTTPS, and every page loops (`ERR_TOO_MANY_REDIRECTS`).
2. Edit the `*` record and switch it to **Proxied**. Renewals keep working: DNS-01 never goes
   through the proxy.
3. Proxy **one level only**. The free certificate at Cloudflare's edge covers `example.dev` and
   `*.example.dev`, not `*.hml.example.dev`: keep deeper records on DNS only. A LAN-only environment
   can't be proxied anyway.
4. **Web UIs**: Cloudflare caches `.js` files by default, and Flutter's `main.dart.js` has no hash
   in its name, so after a deploy browsers can get the previous build. Add a Cache Rule (*Caching →
   Cache Rules*) that bypasses the cache for the UI host names, or purge the cache after a release.

API responses (JSON) aren't cached by default. GitHub's webhooks and the Jenkins agents' WebSockets
work through the proxy.

## Option B: DuckDNS, free

[DuckDNS](https://www.duckdns.org) gives you up to five free subdomains of `duckdns.org`. Each
resolves every name below it too, so `yourname.duckdns.org` also answers for
`heimdall.yourname.duckdns.org`: that is the wildcard. Traefik supports it as a DNS provider.

### B1. Create the subdomains

1. Sign in at [duckdns.org](https://www.duckdns.org) (GitHub, Google, ...).
2. Add one subdomain **per environment**: `yourname` for production, `yourname-hml` for
   homologation. Every name under a subdomain shares its address, so `hml.yourname.duckdns.org`
   can't point at a different host than `yourname.duckdns.org`.
3. Set each subdomain's **current ip** to its host's address and click *update ip*.
4. Copy the **token** at the top of the page. It is one token for the whole account.

### B2. Configure the host

`/etc/yggdrasil/acme.env`:

```
DUCKDNS_TOKEN=<token>
```

`/etc/yggdrasil/platform.env`, the DNS part:

```
DOMAIN=yourname.duckdns.org
ACME_EMAIL=you@example.com
ACME_DNS_PROVIDER=duckdns
ACME_CA_SERVER=https://acme-staging-v02.api.letsencrypt.org/directory
JENKINS_URL=https://jenkins.yourname.duckdns.org/
```

Homologation's host gets `DOMAIN=yourname-hml.duckdns.org`.

These are only the DNS lines. The platform won't start, and even `platform.sh logs` fails, until
the other required variables are filled in too: see
[the checklist](#2-start-the-platform-against-the-staging-ca).

### B3. Know the limits

- **One TXT record per subdomain.** The certificate needs two validations at the same name
  (`DOMAIN` and `*.DOMAIN`). Traefik answers them one after the other for DuckDNS, which is slower
  but works. That's one more reason for the staging CA on the first run.
- **Availability** is whatever the free service gives. When DuckDNS is down, new visitors can't
  resolve your names, and renewals retry until it's back.
- **The token** can change every subdomain of your account: keep `acme.env` at `chmod 600`.
- **Some networks and filters** distrust dynamic-DNS domains.

Moving to your own domain later only means changing `DOMAIN`, the DNS provider and the application
env files: see [Changing the domain later](#changing-the-domain-later).

## Bring it up (either option)

### 1. Open the ports

On the host, and in the VPS provider's panel if it has its own firewall or security group:

```bash
sudo ufw allow OpenSSH && sudo ufw allow 80,443/tcp && sudo ufw enable
```

### 2. Start the platform against the staging CA

`platform.env` configures the whole platform, not only Traefik, and Compose reads all of it on every
`platform.sh` command. So before the first one, check that **every** variable below has a value,
not only the DNS ones. Otherwise it stops with `required variable ... is missing a value`.

| Variable | Value |
|---|---|
| `ENVIRONMENT` | This host's environment id, exactly as in `catalog.yaml`. `python3 /opt/yggdrasil/scripts/catalog.py environments` lists them |
| `DOMAIN`, `ACME_EMAIL`, `ACME_DNS_PROVIDER` | As above |
| `ACME_CA_SERVER` | The staging URL, as above, for this first run |
| `TRAEFIK_DASHBOARD_USERS` | The output of `htpasswd -nB admin`, in single quotes. Replace the template's placeholder |
| `GRAFANA_ADMIN_PASSWORD` | Any password, e.g. the output of `openssl rand -hex 16` |
| `YGGDRASIL_STATUS_TOKEN` | The output of `openssl rand -hex 32`. Keep a copy: the console asks for it |
| `COMPOSE_PROFILES` | `jenkins` on the controller host, with the Jenkins variables of [setup.md step 9.2](setup.md#92-the-env-files-first-pass). **Empty** on another host while you only test DNS: the template's `agent` needs an agent secret that doesn't exist yet |

The last three can be generated straight into the file:

```bash
sed -i "s/^GRAFANA_ADMIN_PASSWORD=.*/GRAFANA_ADMIN_PASSWORD=$(openssl rand -hex 16)/" /etc/yggdrasil/platform.env
sed -i "s/^YGGDRASIL_STATUS_TOKEN=.*/YGGDRASIL_STATUS_TOKEN=$(openssl rand -hex 32)/" /etc/yggdrasil/platform.env
grep -E '^(ENVIRONMENT|DOMAIN|ACME_|TRAEFIK_DASHBOARD_USERS|GRAFANA_ADMIN_PASSWORD|YGGDRASIL_STATUS_TOKEN|COMPOSE_PROFILES)' /etc/yggdrasil/platform.env
```

`/opt/yggdrasil/scripts/platform.sh config > /dev/null` checks the file without starting anything:
no output means it's complete. Then start the platform:

```bash
chmod 600 /etc/yggdrasil/*.env
/opt/yggdrasil/scripts/platform.sh up
/opt/yggdrasil/scripts/platform.sh logs traefik | grep -i acme
```

A minute or two later, the logs should show no ACME errors, and:

```bash
dig +short yggdrasil.example.dev                  # the host's address (Cloudflare's, if proxied)
curl -kI https://yggdrasil.example.dev            # an HTTP answer
curl -kvI https://yggdrasil.example.dev 2>&1 | grep -i issuer   # a "(STAGING)" issuer
```

Staging certificates are untrusted, hence `-k`, but not rate limited: fix anything here, not against
the production CA.

### 3. Switch to the real certificate

Comment `ACME_CA_SERVER` out in `platform.env`, drop the staging certificate and start again:

```bash
/opt/yggdrasil/scripts/platform.sh down
docker volume rm yggdrasil_letsencrypt
/opt/yggdrasil/scripts/platform.sh up
```

`curl -I https://yggdrasil.example.dev` now works without `-k`. Traefik renews the certificate by
itself, 30 days before it expires.

### 4. Point the GitHub App and the applications at it

- **GitHub App** ([setup.md](setup.md#7-create-the-github-app) step 7): Homepage URL `https://jenkins.<DOMAIN>` (only
  informational, any URL works), Webhook URL `https://jenkins.<DOMAIN>/github-webhook/` (must be
  reachable from GitHub, trailing slash included), with the `DOMAIN` of the host that runs the
  controller. It must match `JENKINS_URL`. You can create the app before the domain exists, with the
  webhook inactive, and fill the URLs in later: the App ID and key don't change. Check the result in
  the app's *Advanced → Recent Deliveries*: each delivery should have a green check.
- **Applications**: each env file (`/etc/yggdrasil/<environment>/<application>.env`) sets its host
  names under the environment's `DOMAIN`, matching the `host` in `catalog.yaml`:
  ```
  PUBLIC_HOST=heimdall-api.example.dev
  UI_HOST=heimdall.example.dev
  ```

## Changing the domain later

1. Create the records (or subdomains) and the credential of the new provider.
2. On each host:
   - `platform.env`: `DOMAIN`, `ACME_DNS_PROVIDER`, `JENKINS_URL`, and any URLs in
     `YGGDRASIL_STATUS_CORS_ORIGINS` and `YGGDRASIL_CONSOLE_ENVIRONMENTS`.
   - `acme.env`: the new provider's credentials.
   - Every application env file: `PUBLIC_HOST`, `UI_HOST`, and anything else built from the domain.
3. `scripts/platform.sh up`, then redeploy each application so it picks up its new host names.
4. Update the GitHub App's URLs, and each environment's URL in the Windows and Android consoles.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `required variable ... is missing a value` | A variable of `platform.env` is empty: see [the checklist](#2-start-the-platform-against-the-staging-ca). If it isn't, the file has Windows line endings (`sed -i 's/$//' /etc/yggdrasil/platform.env`), or you ran `docker compose` instead of `platform.sh` |
| `could not find zone` / `zone not found` (Cloudflare) | The token lacks **Zone → Zone → Read**, or its zone resource isn't this domain |
| `Authentication error` / `403` (Cloudflare) | Wrong token, an expired one, or its IP filtering excludes the host |
| `NXDOMAIN` or `incorrect TXT record` | The domain isn't *Active* on Cloudflare yet (name servers), a typo in `DOMAIN`, or, on DuckDNS, the two validations overlapping: retry against staging |
| `rateLimited` | Too many attempts against the production CA. Use the staging CA and wait (the limits reset within hours) |
| The browser shows a Traefik default certificate | The certificate hasn't been issued yet: read the ACME lines in Traefik's logs |
| `ERR_TOO_MANY_REDIRECTS` | Cloudflare proxy with SSL/TLS mode *Flexible*: set **Full (strict)** |
| Timeout on every host name | Ports 80/443 closed on the host or at the provider, or the record points at another address |
| A LAN environment doesn't resolve at home | The router's DNS rebinding protection drops answers with private addresses: allow the domain there, or use hosts-file entries |
| GitHub deliveries fail | `jenkins.<DOMAIN>` isn't reachable from the internet, or the Webhook URL lacks `/github-webhook/` |
