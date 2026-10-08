# DNS and certificates

Every environment served through Traefik (`mode: proxy`) needs a **domain**: `DOMAIN` in its
`platform.env`. Every public host name of that environment is one label under it
(`<name>.<DOMAIN>`). Traefik gets one certificate for `<DOMAIN>` and `*.<DOMAIN>` from Let's
Encrypt, with the DNS-01 challenge. So the DNS provider must:

- resolve `*.<DOMAIN>` to the environment's host, and
- let Traefik create TXT records through an API: the `_acme-challenge` record that proves you own
  the domain.

> **Coming from [setup.md](setup.md) step 6?** Do only **A1 to A4** (Cloudflare) or **B1**
> (DuckDNS) now, then go back to setup.md. You write the host's files (A5 or B2) at setup step 9.2,
> or 10 for a host without the controller, and you start the platform at 9.3. That step sends you
> to [Bring it up](#bring-it-up-either-option) here to check the certificate.

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
| `yggdrasil.<DOMAIN>` | The web console, and the status API under `/api/` |
| `grafana.<DOMAIN>` | Grafana |
| `traefik.<DOMAIN>` | The Traefik dashboard. Answers `401` until you sign in |
| `jenkins.<DOMAIN>` | The Jenkins controller, only on the host whose `COMPOSE_PROFILES` includes `jenkins`. GitHub's webhooks go here |
| `<name>.<DOMAIN>` | Each deployed application, at the `PUBLIC_HOST` or `UI_HOST` of its env file (the same name as its `host` in `catalog.yaml`). Traefik answers `404` until the application is deployed |

A wildcard record covers them all: adding an application never needs a DNS change. The bare
`<DOMAIN>` (`example.dev` itself) needs no record, and nothing is served there, so always test with
a name under it, such as `yggdrasil.<DOMAIN>`.

### The placeholders in this guide

| Placeholder | Replace it with |
|---|---|
| `example.dev` | Your domain |
| `yourname.duckdns.org` | Your DuckDNS subdomain |
| `203.0.113.10` | Your host's public IPv4 address. This one is reserved for documentation and reaches nothing. On the VPS, `curl -4 https://ifconfig.me` prints the real one; the provider's panel shows it too |
| `you@example.com` | Your e-mail address. Let's Encrypt refuses addresses at `example.com` |
| `<token>` | The API token or DuckDNS token you create below |

## Option A: Cloudflare with your own domain

### A1. Get a domain

Either:

- **Buy it on Cloudflare**: dashboard → *Domain Registration → Register Domains*. Sold at cost (a
  `.com` is about US$10.50 a year) and already on your account: skip A2.
- **Buy it at another registrar** (Porkbun, Namecheap, ...). Some TLDs (`.xyz`, `.site`) cost
  US$1–3 the first year; check the renewal price. Then do A2.

### A2. Move its DNS to Cloudflare

Only for a domain bought elsewhere.

1. At the registrar, if **DNSSEC** is on for the domain, turn it off first. Otherwise the domain stops
   resolving when the name servers change. You can turn it on again later, in Cloudflare.
2. Cloudflare dashboard → **Add a domain** → your domain → the **Free** plan.
3. Cloudflare imports the records it finds. Delete any old `A` record named `*` that points somewhere
   else.
4. Cloudflare shows two name servers, like `ada.ns.cloudflare.com` and `bob.ns.cloudflare.com`. At the
   registrar, replace the domain's name servers with those two.
5. Wait until Cloudflare shows the domain as **Active**: minutes, sometimes a few hours. Check from
   any machine:
   ```bash
   nslookup -type=NS example.dev 1.1.1.1
   ```
   It should list the two Cloudflare name servers.

### A3. Add the DNS records

Your domain → **DNS → Records → Add record**, one record per environment:

| Type | Name | IPv4 address | Proxy status |
|---|---|---|---|
| `A` | `*` | The production host's public IP (**not** the example `203.0.113.10`) | **DNS only** (grey cloud) to start |
| `A` | `*.hml` | The homologation host's IP, if you have one | **DNS only**, always |

The name follows the environment's `DOMAIN`: `DOMAIN=example.dev` → record `*`,
`DOMAIN=hml.example.dev` → record `*.hml`. A private address (`192.168.x.x`) is fine for an
environment only used on a LAN: Let's Encrypt never connects to the host, it only reads the TXT
record.

Start with **DNS only**: nothing sits between you and the host while you set things up. The proxy is
[step A7](#a7-optional-the-cloudflare-proxy).

Check it from any machine, a minute later:

```bash
nslookup yggdrasil.example.dev 1.1.1.1
```

It should print your host's IP. `nslookup example.dev` (without a name in front) finds nothing, and
that's expected.

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
- **Zone Resources**: Include · Specific zone · your domain.
- **Client IP Address Filtering**: optional, the IPs of the hosts that use the token.
- **TTL**: no end date. An expired token makes renewals fail, silently, weeks later.

**Continue to summary → Create Token**, and copy the token: Cloudflare shows it only once. Keep it
in your password manager until the host's `acme.env` needs it.

*Zone Read* is not optional: Traefik looks the zone up by name before writing to it. Without it,
issuing fails with an error about not finding the zone. Never use the Global API Key.

Check the token from any machine with `curl` (on Windows, Git Bash):

```bash
export CF_DNS_API_TOKEN=<token>
curl -s -H "Authorization: Bearer $CF_DNS_API_TOKEN" https://api.cloudflare.com/client/v4/user/tokens/verify
curl -s -H "Authorization: Bearer $CF_DNS_API_TOKEN" "https://api.cloudflare.com/client/v4/zones?name=example.dev"
```

The first should answer `"status":"active"`, the second list the zone in `result`. An empty
`result` means Zone Read or the zone resource is wrong. If you set IP filtering, run it from one of
those IPs.

### A5. Configure the host

On the host, once [setup.md step 8](setup.md#8-prepare-every-host) has copied the templates to
`/etc/yggdrasil`.

`/etc/yggdrasil/acme.env`: replace the template's `CF_DNS_API_TOKEN=` line with

```
CF_DNS_API_TOKEN=<token>
```

`/etc/yggdrasil/platform.env`: the DNS lines, with your own values:

```
DOMAIN=example.dev
ACME_EMAIL=you@example.com
ACME_DNS_PROVIDER=cloudflare
ACME_CA_SERVER=https://acme-staging-v02.api.letsencrypt.org/directory
JENKINS_URL=https://jenkins.example.dev/
```

- `ACME_CA_SERVER` is commented out in the template: remove the `#` in front of it. It points
  Traefik at Let's Encrypt's staging service for the first run: see
  [Bring it up](#bring-it-up-either-option).
- `JENKINS_URL` uses the `DOMAIN` of the host that runs the controller, on every host.
- Each environment host has its own `platform.env` with its own `DOMAIN` (`hml.example.dev` for
  homologation). The same token serves every host of the zone.

These are only the DNS lines. The platform won't start until the other required variables are filled
in too: see [the checklist](#2-fill-in-every-required-variable).

### A6. Bring it up

Coming from setup.md: go back to it. Otherwise, continue with
[Bring it up](#bring-it-up-either-option).

### A7. Optional: the Cloudflare proxy

The proxy (orange cloud) hides the host's IP and adds DDoS protection and caching. Turn it on only
once the real certificate works ([step 4](#4-switch-to-the-real-certificate)):

1. **SSL/TLS → Overview**. Set the encryption mode to **Full (strict)**. On newer zones the mode is
   *Automatic*: click **Configure → Custom SSL/TLS → Full (strict)**. Required: with *Flexible*,
   Cloudflare speaks HTTP to Traefik, Traefik redirects to HTTPS, and every page loops
   (`ERR_TOO_MANY_REDIRECTS`).
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
3. Set each subdomain's **current ip** to its host's public IPv4 address and click *update ip*.
4. Copy the **token** at the top of the page. It is one token for the whole account.

Check it from any machine: `nslookup yggdrasil.yourname.duckdns.org 1.1.1.1` prints the host's IP.

### B2. Configure the host

On the host, once [setup.md step 8](setup.md#8-prepare-every-host) has copied the templates to
`/etc/yggdrasil`.

`/etc/yggdrasil/acme.env`: delete the template's `CF_DNS_API_TOKEN=` line and add

```
DUCKDNS_TOKEN=<token>
```

`/etc/yggdrasil/platform.env`: the DNS lines, with your own values:

```
DOMAIN=yourname.duckdns.org
ACME_EMAIL=you@example.com
ACME_DNS_PROVIDER=duckdns
ACME_CA_SERVER=https://acme-staging-v02.api.letsencrypt.org/directory
JENKINS_URL=https://jenkins.yourname.duckdns.org/
```

Remove the `#` in front of `ACME_CA_SERVER`, as in A5. Homologation's host gets
`DOMAIN=yourname-hml.duckdns.org`.

These are only the DNS lines. The platform won't start until the other required variables are filled
in too: see [the checklist](#2-fill-in-every-required-variable).

### B3. Know the limits

- **One TXT record per subdomain.** The certificate needs two validations at the same name
  (`DOMAIN` and `*.DOMAIN`). Traefik answers them one after the other for DuckDNS, which is slower
  but works. That's one more reason for the staging CA on the first run.
- **Availability** is whatever the free service gives. When DuckDNS is down, new visitors can't
  resolve your names, and renewals retry until it's back.
- **The token** can change every subdomain of your account: keep `acme.env` at `chmod 640`, readable by its owner and the `docker` group only.
- **Some networks and filters** distrust dynamic-DNS domains.

Moving to your own domain later only means changing `DOMAIN`, the DNS provider and the application
env files: see [Changing the domain later](#changing-the-domain-later).

## Bring it up (either option)

The first certificate is issued by Let's Encrypt's **staging** service: its certificates are
untrusted, but it isn't rate limited, so you can fix mistakes freely. Then you switch to the real
one.

### 1. Open the ports

Already done if you followed [setup.md step 8](setup.md#8-prepare-every-host). On the host, and in
the VPS provider's panel if it has its own firewall or security group:

```bash
sudo ufw allow OpenSSH && sudo ufw allow 80,443/tcp && sudo ufw enable
sudo ufw status
```

OpenSSH is allowed first, so enabling the firewall doesn't cut your SSH session. `ufw status` should
list 22 (OpenSSH), 80 and 443 as `ALLOW`.

### 2. Fill in every required variable

`platform.env` configures the whole platform, not only Traefik, and Compose reads all of it on every
`platform.sh` command. So check that **every** variable below has a value, not only the DNS ones.
Otherwise `platform.sh` stops with `required variable ... is missing a value`.

| Variable | Value |
|---|---|
| `ENVIRONMENT` | This host's environment id, exactly as in `catalog.yaml`. `python3 /opt/yggdrasil/scripts/catalog.py environments` lists them |
| `DOMAIN`, `ACME_EMAIL`, `ACME_DNS_PROVIDER` | As in A5 or B2 |
| `ACME_CA_SERVER` | The staging URL, as in A5 or B2, for this first run |
| `TRAEFIK_DASHBOARD_USERS` | `admin:` and a password hash, in single quotes. The template's `replace-me` placeholder passes the check but no password works with it |
| `GRAFANA_ADMIN_PASSWORD` | Any password |
| `YGGDRASIL_STATUS_TOKEN` | A random token of at least 32 characters. Keep a copy: the console asks for it |
| `COMPOSE_PROFILES` | **Empty** to test DNS alone, on any host. On the controller host, `jenkins` once [setup.md step 9.2](setup.md#92-the-env-files-first-pass) is done: the GitHub App key must exist first. Not `agent` yet: it needs an agent secret that only exists later |

The secrets can be generated straight into the file:

```bash
f=/etc/yggdrasil/platform.env
hash=$(htpasswd -nB admin)      # asks twice for the Traefik dashboard password you choose
sed -i "s|^TRAEFIK_DASHBOARD_USERS=.*|TRAEFIK_DASHBOARD_USERS='$hash'|" "$f"
sed -i "s|^GRAFANA_ADMIN_PASSWORD=.*|GRAFANA_ADMIN_PASSWORD=$(openssl rand -hex 16)|" "$f"
sed -i "s|^YGGDRASIL_STATUS_TOKEN=.*|YGGDRASIL_STATUS_TOKEN=$(openssl rand -hex 32)|" "$f"
```

- `htpasswd -nB admin` prints `admin:<bcrypt hash>` for the user `admin`. It comes with
  `apache2-utils` ([setup.md step 8](setup.md#8-prepare-every-host)).
- `openssl rand -hex 16` and `-hex 32` print 32 and 64 random hexadecimal characters.
- `sed -i "s|^NAME=.*|NAME=value|" "$f"` replaces the whole `NAME=` line in the file. `|` separates
  the parts because the hash contains `/`.
- The Grafana password and the status token are in the file: `grep` them out when you need them,
  and keep a copy in your password manager.

Check the non-secret lines, and let Compose check the file:

```bash
grep -E '^(ENVIRONMENT|COMPOSE_PROFILES|DOMAIN|ACME_)' /etc/yggdrasil/platform.env
/opt/yggdrasil/scripts/platform.sh config > /dev/null && echo "platform.env OK"
```

`platform.sh config` checks the variables Compose requires, and prints `platform.env OK`. It doesn't
check that `ENVIRONMENT` exists in the catalog, nor the Jenkins variables: `platform.sh up` does,
and stops with `platform: <NAME> must be set ...` before starting anything.

Then lock the files down:

```bash
chmod 640 /etc/yggdrasil/*.env
```

### 3. Start the platform against the staging CA

```bash
/opt/yggdrasil/scripts/platform.sh up
```

The first run builds images and pulls the others: several minutes. It returns once every service is
running (and healthy, for those with a health check), and ends with a table of the services: each `Up`,
`traefik`, `status` and `console` also `(healthy)`. Give Traefik a minute
more to answer the DNS-01 challenge, then read its log:

```bash
docker logs yggdrasil-traefik-1 2>&1 | grep -iE 'acme|error' | tail -20
```

- `docker logs yggdrasil-traefik-1` prints Traefik's log once and returns. (`platform.sh logs traefik`
  follows it live instead: good to watch, stop it with Ctrl+C, but don't pipe it into `grep` and
  wait for it to finish.)
- No lines, or no `error` lines, is good: Traefik logs little when issuing succeeds. Errors are
  explained in [Troubleshooting](#troubleshooting).

Check that the certificate exists:

```bash
docker exec yggdrasil-traefik-1 grep -o '"main": *"[^"]*"' /letsencrypt/acme.json
```

It prints `"main": "example.dev"` (your domain) once the certificate is issued, and nothing before.

Then test it **from your own computer**, not the VPS. Many providers don't route a host's requests
to its own public IP, so `curl` on the VPS times out even when everything works.

```bash
nslookup yggdrasil.example.dev 1.1.1.1                               # your host's IP (Cloudflare's, if proxied)
curl -kI https://yggdrasil.example.dev                               # HTTP/2 200: the console
curl -kvI https://yggdrasil.example.dev 2>&1 | grep -i issuer        # an issuer containing "(STAGING)"
```

`-k` accepts the untrusted staging certificate. To test on the VPS itself, send the request to
`127.0.0.1` while keeping the name:

```bash
curl -kI --resolve yggdrasil.example.dev:443:127.0.0.1 https://yggdrasil.example.dev
```

Always use a host name under the domain. `https://example.dev` has no record and no route, and
`https://203.0.113.10` sends no name, so Traefik refuses the handshake (`unrecognized name`).

Fix anything here, against staging, not against the real CA.

### 4. Switch to the real certificate

1. In `/etc/yggdrasil/platform.env`, put the `#` back in front of `ACME_CA_SERVER=`. Without it,
   Traefik uses Let's Encrypt's production service.
2. Delete the staging certificate, which is stored in the `yggdrasil_letsencrypt` volume, and start
   again:
   ```bash
   /opt/yggdrasil/scripts/platform.sh down
   docker volume rm yggdrasil_letsencrypt
   /opt/yggdrasil/scripts/platform.sh up
   ```
   `down` stops the platform, so the volume is free to delete. The applications keep running.
3. A minute later, from your computer:
   ```bash
   curl -I https://yggdrasil.example.dev                              # no -k: the certificate is trusted
   curl -vI https://yggdrasil.example.dev 2>&1 | grep -i issuer       # Let's Encrypt, without "STAGING"
   ```

Traefik renews the certificate by itself, 30 days before it expires.

### 5. Point the GitHub App and the applications at it

- **GitHub App** ([setup.md step 7](setup.md#7-create-the-github-app)): Homepage URL
  `https://jenkins.<DOMAIN>` (only informational), Webhook URL
  `https://jenkins.<DOMAIN>/github-webhook/` (must be reachable from GitHub, trailing slash
  included), with the `DOMAIN` of the host that runs the controller. It must match `JENKINS_URL`.
  You can create the app before the domain exists, with the webhook inactive, and fill the URLs in
  later: the App ID and key don't change. Check the result in the app's *Advanced → Recent
  Deliveries*: each delivery should have a green check.
- **Applications**: each env file (`/etc/yggdrasil/<environment>/<application>.env`) sets its host
  names under the environment's `DOMAIN`, matching the `host` in `catalog.yaml`. For example:

  | File | Lines |
  |---|---|
  | `heimdall-api.env` | `PUBLIC_HOST=heimdall-api.example.dev` and `UI_HOST=heimdall.example.dev` (the API is also served under its UI's origin) |
  | `heimdall-ui.env` | `UI_HOST=heimdall.example.dev` and `HEIMDALL_API_BASE_URL=https://heimdall.example.dev` |

  Complete examples: [examples/docker-desktop-wsl-vps/env](examples/docker-desktop-wsl-vps/env).

## Changing the domain later

1. Create the records (or subdomains) and the credential of the new provider.
2. On each host:
   - `platform.env`: `DOMAIN`, `ACME_DNS_PROVIDER`, `JENKINS_URL`, and any URLs in
     `YGGDRASIL_STATUS_CORS_ORIGINS` and `YGGDRASIL_CONSOLE_ENVIRONMENTS`.
   - `acme.env`: the new provider's credentials.
   - Every application env file: `PUBLIC_HOST`, `UI_HOST`, and every URL built from the domain. In
     the example applications: `HEIMDALL_API_BASE_URL`, `FORTUNA_API_BASE_URL`,
     `HEIMDALL_EMAIL_VERIFICATION_URL`, `HEIMDALL_PASSWORD_RESET_URL` and
     `HEIMDALL_CORS_ALLOWED_ORIGINS`.
3. `scripts/platform.sh up`, then redeploy each application so it picks up its new host names. Web
   UIs compile their URLs into the bundle, so they must be rebuilt, which a deploy does.
4. Update the GitHub App's URLs, and each environment's URL in the Windows and Android consoles.

## Troubleshooting

Traefik's errors: `docker logs yggdrasil-traefik-1 2>&1 | grep -iE 'acme|error' | tail -20`.

| Symptom | Likely cause |
|---|---|
| `required variable ... is missing a value` | A variable of `platform.env` is empty: see [the checklist](#2-fill-in-every-required-variable). If it isn't, the file has Windows line endings (fix: `sed -i 's/\r$//' /etc/yggdrasil/platform.env`), or you ran `docker compose` instead of `platform.sh` |
| `platform: JENKINS_AGENT_NAME must be set ...` (or another `JENKINS_*`) | `COMPOSE_PROFILES` has `agent` before this host's agent exists. Empty it, or set `jenkins` on the controller host, until the agent exists ([setup.md 9.5](setup.md#95-the-env-files-second-pass-this-hosts-agent)) |
| `client version 1.24 is too old` in Traefik's log, and no certificate | A Traefik older than v3.6.1 on Docker Engine 29 or later: it sees no containers, so no routes and no certificate. `git pull` in `/opt/yggdrasil`, then `scripts/platform.sh up` |
| `unknown TLS options: default@file` in Traefik's log; `schannel: failed to receive handshake` or `unexpected eof` from `curl` | A checkout before v0.3.2 refers to the TLS options by an unknown name, so Traefik builds no HTTPS route. `git pull` in `/opt/yggdrasil`, then `scripts/platform.sh up` |
| `invalidContact` / `contact email has forbidden domain` | `ACME_EMAIL` is still an example address: use your own |
| `could not find zone` / `zone not found` (Cloudflare) | The token lacks **Zone → Zone → Read**, or its zone resource isn't this domain |
| `Authentication error` / `403` (Cloudflare) | Wrong token, an expired one, or its IP filtering excludes the host |
| `some credentials information are missing` | `acme.env` lacks the variable `ACME_DNS_PROVIDER` needs (`CF_DNS_API_TOKEN`, `DUCKDNS_TOKEN`...), or it's misspelled |
| `NXDOMAIN` or `incorrect TXT record` | The domain isn't *Active* on Cloudflare yet (name servers), a typo in `DOMAIN`, or, on DuckDNS, the two validations overlapping: retry against staging |
| `rateLimited` | Too many attempts against the production CA. Use the staging CA and wait (the limits reset within hours) |
| `unrecognized name` TLS alert, or `SEC_E_ILLEGAL_MESSAGE` on Windows | Traefik has no certificate for that name and refuses unknown names (`sniStrict`). Either the certificate isn't issued yet (read Traefik's errors), or the request used an IP address or a name outside `DOMAIN`/`*.DOMAIN` |
| `Could not resolve host` for `example.dev` itself | Expected: only `*.example.dev` has a record. Test `yggdrasil.example.dev` |
| Timeout from the VPS itself, but it works from your computer | The provider doesn't route the host's requests to its own public IP. Test on the VPS with `curl --resolve <name>:443:127.0.0.1` |
| Timeout on every host name, from everywhere | Ports 80/443 closed on the host or at the provider, or the record points at another address (check it isn't the example `203.0.113.10`) |
| `ERR_TOO_MANY_REDIRECTS` | Cloudflare proxy with SSL/TLS mode *Flexible*: set **Full (strict)** |
| A LAN environment doesn't resolve at home | The router's DNS rebinding protection drops answers with private addresses: allow the domain there, or use hosts-file entries |
| GitHub deliveries fail | `jenkins.<DOMAIN>` isn't reachable from the internet, or the Webhook URL lacks `/github-webhook/` |
