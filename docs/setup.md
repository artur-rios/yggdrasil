# Setting up yggdrasil

This guide takes you from nothing to a first release deployed by yggdrasil. Follow the steps in
order: each one only needs what the previous ones produced. The path it follows is this
repository's default setup: **two machines**, a dev machine (the Jenkins controller and the git
server) and an apps machine (development, homologation and production), plus a `local` environment
on your workstation. Other layouts (everything on one machine, one host per environment, or
anything in between) change only a few answers of step 0. What the two machines are, who the users
on them are and why the installer does what it does is in [hosts.md](hosts.md). An older example
installation, with the controller on the same VPS as the environments, is in
[examples/docker-desktop-and-vps](examples/docker-desktop-and-vps/README.md).

```mermaid
flowchart TB
    subgraph prepare [Prepare, on your machine]
        S0[0 · Plan] --> S1[1 · Tools] --> S2[2 · Fork] --> S3[3 · Catalog] --> S4[4 · Stack files] --> S5[5 · Application repositories]
    end
    subgraph accounts [Accounts]
        S6[6 · Domain and DNS] --> S7[7 · GitHub App]
    end
    subgraph hosts [Hosts]
        S8[8 · Prepare each machine] --> S9[9 · Dev machine] --> S10[10 · Apps machine] --> S11[11 · Application env files]
    end
    subgraph finish [Finish]
        S12[12 · Branch rules] --> S13[13 · Check everything] --> S14[14 · First release]
    end
    prepare --> accounts --> hosts --> finish
```

| Step | Where | Time |
|---|---|---|
| [0. Plan the installation](#0-plan-the-installation) | Paper | 10 min |
| [1. Install the tools](#1-install-the-tools-on-your-machine) | Your machine | 10 min |
| [2. Fork yggdrasil](#2-fork-yggdrasil) | GitHub, your machine | 5 min |
| [3. Write the catalog](#3-write-the-catalog) | Your machine | 20 min |
| [4. Write the stack files](#4-write-the-stack-files) | Your machine | 10 min per application |
| [5. Prepare each application repository](#5-prepare-each-application-repository) | Each application repository | 10 min per application |
| [6. Domain and DNS](#6-domain-and-dns) | DNS provider | 15 min, plus propagation |
| [7. Create the GitHub App](#7-create-the-github-app) | GitHub | 10 min |
| [8. Prepare each machine](#8-prepare-each-machine) | Each machine | 20 min per machine |
| [9. The dev machine](#9-the-dev-machine) | Dev machine | 30 min |
| [10. The apps machine](#10-the-apps-machine) | Apps machine, and other hosts | 15 min per host |
| [11. Application env files](#11-application-env-files) | Each host | 5 min per application |
| [12. Apply the branch rules](#12-apply-the-branch-rules) | Your machine | 5 min |
| [13. Check everything](#13-check-everything) | Browser | 10 min |
| [14. The first release](#14-the-first-release) | An application repository | 10 min |

## 0. Plan the installation

Decide these before touching anything, and write the answers down: every later step uses them.

**Moving an installation that runs today** (one VPS that deploys from GitHub) to this setup: follow
[cutover.md](cutover.md) rather than this guide straight through. It says which of these steps to do, in which
order and with which answers, and adds the maintenance window that moves the data and the repositories.

| Decision | Example | Notes |
|---|---|---|
| **GitHub owner** | `acme` | The user or organisation that owns the application repositories. Your fork of yggdrasil goes there too. |
| **Environments**, in promotion order | `local`, `development`, `homologation`, `production` | For each: its `trigger` (`manual`, `branch` or `release`), its `mode` (`proxy`, or `ports` for a workstation), and whether it runs only while used (`onDemand`). See [catalog.md](catalog.md#environments). |
| **Machines**, their roles and the environments each runs | a **dev** machine (the Jenkins controller and Forgejo) and an **apps** machine (`development`, `homologation`, `production`); `local` on your workstation | Each machine is an Ubuntu 24.04 or 26.04 LTS server with a role: `dev` or `apps` ([The two machines](hosts.md#1-two-machines)). It runs one environment or several: environments sharing a host share its platform stack, its Jenkins agent and its domain. A `ports` environment (a workstation) needs no host setup. See [Planning the hosts](#planning-the-hosts). |
| **The controller host** | the dev machine | The one host that runs the Jenkins controller. It must be reachable from the internet on 443, for GitHub's webhooks (Forgejo's come from the same machine), and from the other hosts' agents. |
| **Domain per machine** | `example.com` | Every public name is one label under it: `heimdall.example.com`, and in an environment with a `hostSuffix`, `heimdall-dev.example.com`. A machine can serve more than one domain (`EXTRA_DOMAINS`). Your own domain on Cloudflare, or a free DuckDNS subdomain: [dns.md](dns.md). |
| **Systems and applications** | `shop` = `shop-api` + `shop-web` | Each application is one repository with a Dockerfile and a `/health`-style endpoint. |

A minimal installation is **one** environment (`production`, `trigger: release`) on **one** machine
that also runs the controller. Start there if you are trying yggdrasil out: it is a machine with
role `apps` whose `COMPOSE_PROFILES` also has `jenkins` (and, once the controller has made the
agent's secret, `agent`), as step 9.5 describes. Adding an environment to that host later is a
catalog entry and one line of its `platform.env` ([Adding things](#an-environment)).

### Planning the hosts

| Layout | Catalog | Each host |
|---|---|---|
| **Two machines** (the default) | As in the next row, for the apps machine. The dev machine runs no environment | Dev machine: `--role dev`, profiles `forge,jenkins`, no `ENVIRONMENTS`. Apps machine: `--role apps`, profiles `apps,agent`. [hosts.md](hosts.md) |
| **One machine for everything** | As in the next row | `--role apps`, profiles `apps,jenkins` and then `apps,jenkins,agent`: the controller and the environments share one machine and one engine. Still supported, behind Cloudflare's proxy (the `apps` role opens 80 and 443 to Cloudflare only). It has none of the dev-machine users; the `forge` profile also works there if you want Forgejo. A host without Cloudflare in front is not covered by `ygg install`: set it up by hand ([step 8](#8-prepare-each-machine) lists what to do) and open 80 and 443 in `ufw` yourself |
| **Several environments on one host** | The environments of the host set the same `agent` (`vps`) and each a distinct `hostSuffix` (`-dev`, `-hml`; production can keep none). `onDemand: true` on those used only now and then | One platform stack; `ENVIRONMENTS` in its `platform.env` lists them all; one `DOMAIN`, one `*.DOMAIN` DNS record and certificate for all of them |
| **One environment per host** | `hostSuffix` empty; `agent` left out (it defaults to the environment's id) | One platform stack each, with `ENVIRONMENTS` naming its one environment, and its own `DOMAIN` (`example.com`, `staging.example.com`), each with its own `*.DOMAIN` record |
| **Mixed** | Production on a host of its own; development and homologation sharing another, with their own `agent` and suffixes | As above, per host |

Every application runs in each environment as its own Compose project, `<application>-<environment>`,
so environments on one Docker engine never touch each other
([catalog.md](catalog.md#several-environments-on-one-host)). Two `ports` environments on one
workstation would publish the same ports, though: keep one per machine.

### What each host needs

- **Ubuntu 24.04 or 26.04 LTS** for the `dev` and `apps` roles (the installer refuses another version): a
  VPS or a VM, with an admin user that has `sudo` and logs in with an SSH key. Docker Engine, the
  Compose plugin (v2.24 or later, for `!reset` in the overlays) and the rest come from
  `ygg install` ([step 8](#8-prepare-each-machine)).
- Roughly **2 GB of RAM** for the platform, **4 GB** on the host that also runs the Jenkins
  controller, plus what your applications need in every environment that runs at the same time. A
  stopped on-demand environment uses no memory. The dev machine also runs Forgejo and the checks:
  see [hosts.md](hosts.md#1-two-machines) for its sizing.
- `git`, `bash`, `openssl`, and `python3` with PyYAML (the installer adds them).
- Inbound ports, set by the installer: **dev** 22, 80, 443 and 2222 (Forgejo's git over SSH);
  **apps** 22, and 80 and 443 only from Cloudflare's address ranges. Nothing else inbound.

## 1. Install the tools on your machine

On the machine you work from (Linux, macOS, or Windows with Git Bash):

| Tool | Why | Install |
|---|---|---|
| `git` | Everything | [git-scm.com](https://git-scm.com/downloads) |
| `python3` + PyYAML | Validating the catalog, applying the branch rules | `pip install pyyaml` (Ubuntu: `sudo apt install python3-yaml`) |
| GitHub CLI `gh` | Applying the branch rules on GitHub (not needed with Forgejo: there a token does it, step 12) | [cli.github.com](https://cli.github.com), then `gh auth login` as an admin of the repositories |
| `openssl` | Generating tokens, converting the GitHub App key | Included with Git Bash, macOS and most Linux distributions |
| `ssh` | Reaching the hosts | Included |

Check them:

```bash
git --version && python3 -c "import yaml; print('PyYAML ok')" && openssl version
```

And, for the GitHub rules only (with Forgejo you need no `gh`; step 12 uses a token):

```bash
gh auth status
```

On Windows, use `python` if `python3` isn't on the path.

## 2. Fork yggdrasil

Your installation lives in your own copy of this repository: your catalog, your stack files. Its
**`main`** branch is what counts: every host runs a checkout of it, and every deploy clones it
([details](#where-jenkins-and-the-hosts-read-the-catalog)).

The [license](../LICENSE) allows this copy: you may fork or import the repository, change it and
run it on hosts you control to deploy your own applications. You may not redistribute it or offer
it to others as a service.

This repository lives at [git.techcraftsmen.net/artur-rios/yggdrasil](https://git.techcraftsmen.net/artur-rios/yggdrasil)
(`https://git.techcraftsmen.net/artur-rios/yggdrasil.git`); [github.com/artur-rios/yggdrasil](https://github.com/artur-rios/yggdrasil)
is a read-only mirror of it, with the same branches, tags and releases.

1. On GitHub, **Fork** the mirror into your owner (step 0), and untick *Copy the `develop`
   branch only*: this repository's default branch is `develop`, its work in progress, while
   releases are merged into `main`, the branch your installation runs. For a private copy, use
   *Import repository* (from either address) or create an empty private repository and push `main` to it:
   forks of public repositories can't be private. On a Forgejo of your own, **New migration** from
   `https://git.techcraftsmen.net/artur-rios/yggdrasil.git` does the same.
2. Clone its `main` branch:
   ```bash
   git clone -b main https://github.com/<owner>/<repository>.git
   cd <repository>
   ```

## 3. Write the catalog

[`catalog.yaml`](../catalog.yaml) describes the installation. The one in the repository is a
working example ([examples/docker-desktop-and-vps](examples/docker-desktop-and-vps/README.md)):
replace it with yours. Full reference: [catalog.md](catalog.md).

1. **Top level**: `owner` is your GitHub owner. `repository` is your fork's name (omit it if it's
   `yggdrasil`).
2. **`environments`**: the ones from step 0, in promotion order. The shipped ones are the default
   layout, a workstation and one VPS:
   ```yaml
   environments:
     - id: local
       name: Local
       mode: ports
       trigger: manual
     - id: development
       name: Development
       trigger: branch
       branches: develop
       agent: vps
       hostSuffix: -dev
       onDemand: true
     - id: homologation
       name: Homologation
       trigger: branch
       branches: release/*
       agent: vps
       hostSuffix: -hml
       onDemand: true
     - id: production
       name: Production
       trigger: release
       agent: vps
   ```
   For one host per environment, drop `agent` and `hostSuffix` ([Planning the hosts](#planning-the-hosts)).
   Add `approval: true` to an environment that should wait for someone to click **Deploy**.
3. **`systems`**: replace `heimdall` and `fortuna` with your systems and applications. For each
   application:
   - `id`: the repository name (or set `repository`).
   - `kind`: `api`, `web` or `worker`.
   - `health`: the URL the status API probes over the Docker network, `http://<id>:<port>/<path>`.
     Write the plain id: in each environment the status API probes `<id>.<environment>`.
   - `host`: its public host name under `DOMAIN`, if it has one. Each environment adds its
     `hostSuffix`.
   - `metrics`: `<id>:<port>`, if it exposes Prometheus metrics.
   - `checks`: the names of the checks that run on **every** pull request of that repository and
     that Jenkins runs: each is defined under `checks` in the repository's `yggdrasil.yml` (step 5).
     Don't list `branch-policy`: it's always required. For a GitHub repository that has no
     `yggdrasil.yml` yet, they are the GitHub Actions jobs: look them up in a recent pull request's
     *Checks* tab, which shows `<workflow> / <job>`, and write only the job part (`test`, not
     `CI / test`).
   - `actionsChecks`: the jobs that stay in GitHub Actions because Jenkins cannot run them (Windows,
     macOS), by the same name rule. Required on `develop`, and waited for before a release.
4. **Keep the `yggdrasil` system** at the end as it is: it makes the console show the platform's
   own health.
5. Validate, and see what each application will do where:
   ```bash
   python3 scripts/catalog.py validate
   python3 scripts/catalog.py plan <application-id>
   ```

## 4. Write the stack files

`scripts/deploy.sh` combines Compose files from `stacks/` with the application's repository. The
ones in `stacks/` are working examples: copy the closest one and rename what it names.

| The application's repository | Files to add in `stacks/` |
|---|---|
| **Has its own `docker-compose.yml`** | `<id>.proxy.yml`: Traefik labels, the `edge` and `telemetry` networks with the alias `<id>.${YGG_ENVIRONMENT}`, and `ports: !reset []`. Example: [heimdall-api.proxy.yml](../stacks/heimdall-api.proxy.yml). |
| **Has only a Dockerfile** | `<id>.yml`: the service, built from `${APP_DIR}` and tagged `<id>:${IMAGE_TAG}`. Plus `<id>.proxy.yml`, and `<id>.ports.yml` if a `ports` environment deploys it. Examples: [heimdall-ui.yml](../stacks/heimdall-ui.yml) and its overlays. |

Rules to keep:
- Name everything the environments of one host share after the environment too, from
  `${YGG_ENVIRONMENT}`, which `deploy.sh` exports: the network alias `<id>.${YGG_ENVIRONMENT}`,
  and the Traefik routers and services `<id>-${YGG_ENVIRONMENT}`. Write the labels as a list
  (`- traefik.http.routers.<id>-${YGG_ENVIRONMENT}.rule=...`): Compose substitutes variables in
  values, not in mapping keys.
- Tag the images the stack builds `<id>:${IMAGE_TAG}`: `deploy.sh` sets it to
  `<environment>-<version>`, and rolls back and prunes only those.
- In `proxy` mode, nothing publishes a host port: only Traefik does.
- Host names come from the env file (`PUBLIC_HOST`, `UI_HOST`), not hard-coded. For a site, `deploy.sh`
  also exports `YGG_PUBLIC_HOST`, `YGG_PUBLIC_PATH` and, for an `apex` site with a `host`, `YGG_APEX_HOST` (the name its
  bare-domain redirect answers on) from the catalog's `host`, `domain`, `path` and `apex`
  ([catalog.md](catalog.md#application-fields)), which a stack file may use instead. An `apex` site without a
  `host` is on the bare domain in every environment: deploy it to one environment only.
- To give a build the commit and the environment (what Vercel's `VERCEL_GIT_COMMIT_SHA` and `VERCEL_ENV` were),
  pass `GIT_COMMIT` (the whole SHA of the checkout) and `YGG_ENVIRONMENT` in `build.args`: `deploy.sh` exports both.
  [The site templates](catalog.md#templates-for-repositories) do.
- Use named volumes, not relative bind mounts (`./data:/data`). Under Jenkins, Compose runs inside
  the agent container, so a relative path points into the agent's workspace, not the host.

Delete the example stack files of applications you removed from the catalog. Then check and publish:

```bash
python3 scripts/catalog.py validate
git add -A && git commit -m "chore: describe this installation" && git push origin main
```

Push to **`main`**: it's the branch Jenkins and the hosts read.

## 5. Prepare each application repository

For every application in the catalog:

1. **A Dockerfile** that builds the application, and a **health endpoint** matching the catalog's
   `health`. Give the container a health check (`HEALTHCHECK` in the Dockerfile, or `healthcheck:`
   in the Compose file): `deploy.sh` waits for it to report healthy, and rolls back if it doesn't.
2. **A `develop` branch.** If the repository has none:
   ```bash
   git switch main && git pull && git switch -c develop && git push -u origin develop
   ```
3. **The template files** from [`templates/application/`](../templates/application), on
   `develop`:
   - `CONTRIBUTING.md`: replace `<owner>` and `<repository>` (your fork of yggdrasil) and
     `<application-id>`. It has changes recorded in a `CHANGELOG.md`: if the repository has none,
     start one with a `## [Unreleased]` heading
     ([Keep a Changelog](https://keepachangelog.com/en/1.1.0/)).
   - `.github/workflows/branch-policy.yml`: only for a GitHub repository that has no `yggdrasil.yml`;
     with one, Jenkins runs the branch policy.
   - `yggdrasil.yml`: the starting point of step 4.
   - No `Jenkinsfile`: Jenkins never reads a repository's `Jenkinsfile` ([why](#how-a-build-runs)).
     One that is there (an earlier template's) is ignored; delete it.
4. **A `yggdrasil.yml`** at the repository's root (from the template: replace `<application-id>`, then
   keep the fields you need), next to a `ci/` folder of scripts: the file says
   which image and command run each check (`run: ci/test.sh`), and the scripts are what you can run
   on your own machine too. Its checks are the names you listed in the catalog's `checks` in step 3,
   and its `release` and `publish` sections describe the files of a release and a library's NuGet
   packages. Reference: [catalog.md](catalog.md#repository-pipeline-file). Without the file, the
   repository only deploys, and on GitHub its release waits for every GitHub Actions check as it
   always did. A GitHub repository that already has Actions workflows can move to the file one
   repository at a time: [An existing GitHub installation](#an-existing-github-installation).
5. Commit and push to `develop`. Until the rules are applied (step 12) a direct push works; after,
   only pull requests do.

## 6. Domain and DNS

Follow [dns.md](dns.md) up to and including **A4** (Cloudflare) or **B1** (DuckDNS). With the default
two machines you end up with **two domains on Cloudflare** (the applications' and the sites'; one
domain also works) and:

- on each domain, a `*.<DOMAIN>` record and the bare-domain record for the **apps machine**'s public
  IP, **proxied** (orange cloud). One wildcard covers every environment of the machine:
  `heimdall-dev.<DOMAIN>`, `heimdall-hml.<DOMAIN>` and `heimdall.<DOMAIN>` are all one label under it;
- on the application domain, records of their own for the **dev machine**'s names, `git`, `jenkins`,
  `traefik-dev` and `grafana-dev`, pointing at its IP and **DNS only** (grey cloud): git over SSH cannot pass Cloudflare's
  proxy. A specific record wins over the wildcard ([hosts.md, Domains and Cloudflare](hosts.md#2-domains-and-cloudflare));
- SSL/TLS mode **Full (strict)** on both zones;
- one API token, with **Zone Read** and **DNS Edit** on both zones, which goes into each machine's
  `acme.env` at step 9.2 or 10. For DuckDNS: its token.

Check each name resolves, from any machine:

```bash
nslookup yggdrasil.<DOMAIN> 1.1.1.1
nslookup jenkins.<DOMAIN> 1.1.1.1
```

The first prints a Cloudflare address while the record is proxied (the proxy hides the machine's
IP); the second prints the dev machine's own. Test a name **under** the domain.

The controller's address will be `https://jenkins.<DOMAIN>`, with the `DOMAIN` of the machine that
runs it. Note it down: steps 7 and 9 need it.

## 7. Create the GitHub App

Jenkins talks to GitHub as a GitHub App, not as you. So it doesn't inherit your ruleset bypass, and
its merges go through the same rules as everyone's.

By the end of this step you have two things, both needed in step 9:

| What | Where it goes |
|---|---|
| The app's **App ID**, a number like `1234567` | `GITHUB_APP_ID` in the controller's `platform.env` |
| The app's **private key**, a `.pem` file | `/etc/yggdrasil/github-app.pem` on the controller host |

Create the app under the **same owner** as the repositories (the `owner` in `catalog.yaml`): your
personal account, or the organisation.

### 7.1 Open the form

| Owner | Path | Direct link |
|---|---|---|
| Personal account | Profile picture (top right) → **Settings** → **Developer settings** (last item of the left sidebar) → **GitHub Apps** → **New GitHub App** | `https://github.com/settings/apps/new` |
| Organisation | The organisation's page → **Settings** → **Developer settings** → **GitHub Apps** → **New GitHub App** | `https://github.com/organizations/<owner>/settings/apps/new` |

Creating an app in an organisation needs you to be one of its owners. GitHub may ask for your
password or a two-factor code before showing the form.

### 7.2 Fill in the form

The form is one long page. Go through it top to bottom:

**Basic information**

| Field | Value | Why |
|---|---|---|
| GitHub App name | Anything unique on GitHub, e.g. `acme-yggdrasil` | Shown as the author of Jenkins' merges, tags and statuses |
| Description | Optional, e.g. `Deploys with yggdrasil` | |
| Homepage URL | `https://jenkins.<DOMAIN>` | Required by the form, only informational. `<DOMAIN>` is the controller host's domain, from step 6 |

**Identifying and authorizing users**, and **Post installation**: leave every field empty and every
box unchecked. Jenkins acts as the app, never on behalf of a user.

**Webhook**

| Field | Value | Why |
|---|---|---|
| Active | **Unchecked for now** | The controller doesn't exist yet, and GitHub would pile up failed deliveries. Step 9.4 checks it |
| Webhook URL | `https://jenkins.<DOMAIN>/github-webhook/`, trailing slash included | Where GitHub notifies Jenkins of pushes and pull requests. Fill it in now if the form allows; otherwise in step 9.4 |
| Webhook secret | Empty | |
| SSL verification | Enabled | The controller has a real certificate by the time the webhook is active |

**Permissions → Repository permissions.** Expand the section, and set exactly these; leave every
other one at *No access*:

| Permission | Access | What Jenkins does with it |
|---|---|---|
| Contents | Read and write | Checks the code out, merges the release pull request, creates the `vx.y.z` tag and release, deletes the release branch |
| Actions | Read and write | Starts the application's release-artifact workflow after deployment and waits for its verified draft release to be published |
| Pull requests | Read and write | Finds release pull requests and merges them |
| Commit statuses | Read and write | Sets the `deploy/<environment>` statuses that `main`'s ruleset requires |
| Checks | Read-only | Waits for every GitHub Actions check of a release pull request before deploying |
| Metadata | Read-only | Selected automatically: every app has it |

**Organization permissions** and **Account permissions**: leave them all at *No access*.

**Subscribe to events.** This list only shows events the permissions above allow, so set the
permissions first. Check:

| Event | Why |
|---|---|
| Push | A pushed branch that matches an environment's `branches` deploys there |
| Pull request | A `release/x.y.z → main` pull request starts the release |
| Repository | Jenkins notices renamed or deleted repositories |

If the events can't be checked while the webhook is inactive, check them in step 9.4 when you
activate it.

**Where can this GitHub App be installed?** **Only on this account.** Nobody else should install
your deploy app.

Click **Create GitHub App**.

### 7.3 Note the App ID

GitHub opens the app's **General** page. In the **About** section at the top, copy the **App ID**
(a number). Don't confuse it with the **Client ID** just below it, which starts with `Iv`: Jenkins
doesn't use the client ID.

You can come back to this page any time: **Developer settings → GitHub Apps → Edit** next to the app.

### 7.4 Generate the private key

1. On the same **General** page, scroll to **Private keys** at the bottom.
2. Click **Generate a private key**. Your browser downloads a file named like
   `acme-yggdrasil.2026-09-25.private-key.pem`.
3. Keep it somewhere safe until step 9.1 copies it to the controller host, then delete your copy.

The key lets anyone act as the app on every repository it's installed on: never commit it, and
never paste it in a chat or an issue. GitHub keeps only its public half, so it can't show you the
key again. If you lose it or it leaks, generate a new one here and **Delete** the old one: the App ID
stays the same, only `/etc/yggdrasil/github-app.pem` changes.

### 7.5 Install the app on the repositories

Creating the app gives it no access to anything yet: installing it does.

1. In the app's settings, click **Install App** in the left sidebar.
2. Click **Install** next to your owner (the account or organisation from `catalog.yaml`).
3. Choose **Only select repositories** and pick:
   - every application repository in the catalog (`python3 scripts/catalog.py applications --deployable`
     lists their ids; an application with a `repository` field uses that name);
   - your fork of yggdrasil: Jenkins loads its pipeline library from it.
4. GitHub lists the permissions from 7.2. Click **Install**.

To change the repositories later (when you add an application, say), go to the owner's
**Settings** → **Applications** → **Installed GitHub Apps** (for an organisation: **Settings** →
**GitHub Apps**) → **Configure** next to the app → **Repository access**. A repository the app
isn't installed on gets no Jenkins job branches and never deploys.

## 8. Prepare each machine

On **each** machine: the dev machine and the apps machine, or the one machine of a single-machine
setup. What you get, and why each piece exists, is in [hosts.md](hosts.md): read its
[Users](hosts.md#3-users) and [System settings](hosts.md#4-system-settings) once.

You need, before you start:

- A fresh **Ubuntu 24.04 or 26.04 LTS** server.
- An **admin user** on it (this guide calls it `artur`) with `sudo`, which you log in as over SSH **with a
  key**. Your public key must be in the admin's `~/.ssh/authorized_keys`. Test it from your own
  computer, with passwords refused on your side, so that a login falling back to a password cannot pass for
  a key:

  ```bash
  ssh -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no artur@<host> true && echo key ok
  ```

  It must print `key ok`. The installer switches passwords off, and refuses to start if there is no key to let
  you back in, but it cannot tell whether the key there is yours. An admin made by copying root's
  `authorized_keys` gets whatever key the provider put on root, which may not be yours: add your own
  (`ssh-copy-id artur@<host>`, or append the line of your `~/.ssh/id_ed25519.pub`) and test again.
- A **second SSH session** open while you install, in case something locks you out.

### 8.1 Get the checkout

As the admin:

```bash
sudo apt update && sudo apt install -y git python3 python3-yaml
sudo install -d -o "$USER" /opt/yggdrasil
git clone -b main https://github.com/<owner>/<repository>.git /opt/yggdrasil
```

- `apt install git python3 python3-yaml`: the little the installer itself needs to start. It installs the
  rest.
- `install -d -o "$USER" /opt/yggdrasil`: creates the directory, owned by you for now (`$USER` is filled
  in by the shell; type it as is). The installer hands it to `ygg`. Keep the path: the rest of this guide
  uses it, and so do the systemd units of the backup.
- `git clone -b main …`: your fork's `main` branch, where your catalog is. `<owner>` is the `owner` of
  `catalog.yaml` and `<repository>` your fork's name (`yggdrasil` unless you renamed it). For example:
  `git clone -b main https://github.com/acme/yggdrasil.git /opt/yggdrasil`.

A **private** fork needs a credential. Create a read-only
[deploy key](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys)
for this machine and clone over SSH instead:

```bash
ssh-keygen -t ed25519 -N "" -C "yggdrasil@$(hostname)" -f ~/.ssh/id_ed25519
cat ~/.ssh/id_ed25519.pub
```

- `-t ed25519`: the key type. `-N ""`: no passphrase, so `git pull` works unattended.
- `-C "yggdrasil@$(hostname)"`: a label that tells you, on GitHub, which machine the key belongs to.
- `-f ~/.ssh/id_ed25519`: where the key is written. That is SSH's default key, so `git` uses it
  without further configuration. If the file already exists, skip `ssh-keygen` and use it.

Paste the printed public key in your fork → **Settings → Deploy keys → Add deploy key**, with
*Allow write access* unchecked. Then clone with the SSH URL:
`git clone -b main git@github.com:<owner>/<repository>.git /opt/yggdrasil`. After the install the checkout
belongs to `ygg`, who pulls from then on: repeat the key steps as `ygg`, and add that key to the fork too.

### 8.2 Look first

```bash
cd /opt/yggdrasil
scripts/ygg.sh check --role dev        # or: --role apps
```

- `scripts/ygg.sh`: the `ygg` command, run from the checkout. Once step 8.4 has installed it, `ygg` works from
  any directory.
- `check`: reads the machine and prints one row per piece, `ok`, `warn` or `missing`, and changes
  nothing. A new machine has mostly `missing` rows.
- `--role dev` or `--role apps`: the role to check against. Without it, `check` uses the role the install
  wrote to `/etc/yggdrasil-role`, and `workstation` (today's checks for a laptop or an older host) if
  there is none.

### 8.3 Install

```bash
scripts/ygg.sh install --role dev      # or: --role apps
```

Run it as the admin, not as `root` (it refuses root) and not with `sudo`: it asks `sudo` itself for each
thing that needs it. It checks the machine, then goes through the parts below **in this order**, saying
what it is about to do and asking `Do this?` before each. It skips what is already right, checks the
machine again after each part, and is safe to run again. If you say no to a part, it moves on to the next.
The files it writes, with their content, are in [hosts.md, System settings](hosts.md#4-system-settings).

Next to each part is what you would type to do it by hand. Every command is run as the admin with `sudo`.

1. **Packages.** Docker's `apt` repository ([docs.docker.com/engine/install](https://docs.docker.com/engine/install/ubuntu/)),
   then:
   ```bash
   sudo apt install -y git curl openssl python3 apache2-utils ca-certificates python3-yaml python3-cryptography \
     uidmap dbus-user-session slirp4netns ufw rsync unattended-upgrades \
     docker-ce docker-ce-cli containerd.io docker-ce-rootless-extras docker-buildx-plugin docker-compose-plugin
   ```
   - `uidmap`, `slirp4netns`, `dbus-user-session` and `docker-ce-rootless-extras`: what rootless Docker needs.
   - `apache2-utils`: `htpasswd`, for the Traefik dashboard password. `python3-yaml`: PyYAML, which
     `scripts/catalog.py` needs. `python3-cryptography`: for the [variables store](variables.md).
   - `rsync`: the nightly backup. `ufw`: the firewall. `unattended-upgrades`: part 4.
   - Ubuntu's own `docker.io`, `docker-compose` and `containerd`/`runc` packages clash with Docker's.
     The installer removes them if you agree. By hand:
     `sudo apt remove docker.io docker-doc docker-compose docker-compose-v2 podman-docker containerd runc`.
     Images, containers and volumes stay.
2. **Turn off the machine's own Docker.**
   ```bash
   sudo systemctl disable --now docker.service docker.socket containerd.service
   sudo systemctl mask docker.service docker.socket containerd.service
   sudo gpasswd -d <user> docker        # once for each member of: getent group docker
   ```
   - `disable --now`: stops them and keeps them from starting at boot.
   - `mask`: makes them impossible to start, even by an upgrade of `docker-ce`. The Docker packages start the
     rootful engine; it would answer on `/var/run/docker.sock` to anyone in the `docker` group, which is root.
   - `gpasswd -d <user> docker`: removes a user from the group. Nobody should be in it.
3. **System settings.** The sysctl, AppArmor and systemd files of
   [hosts.md](hosts.md#4-system-settings) (`/etc/sysctl.d/90-yggdrasil.conf`,
   `/etc/apparmor.d/usr.bin.rootlesskit`, `/etc/systemd/system/user@.service.d/delegate.conf`; the AppArmor
   one only where the system ships none, which 26.04 does), then:
   ```bash
   sudo sysctl --system
   sudo systemctl restart apparmor.service
   sudo systemctl daemon-reload
   ```
   - `sysctl --system`: loads the new setting, which lets a non-root process bind ports 80 and 443.
   - `restart apparmor.service`: loads the profile that allows rootless Docker's user namespaces on
     Ubuntu 24.04 and 26.04.
4. **Unattended upgrades.** The file `/etc/apt/apt.conf.d/20auto-upgrades` of hosts.md: security updates
   install themselves.
5. **Service users.** `ygg` on an apps machine; `ygg`, `ci`, `release`, `renovate` and `dev` on a dev machine:
   ```bash
   sudo useradd --create-home --user-group --shell /bin/bash ygg
   sudo useradd --create-home --user-group --shell /usr/sbin/nologin ci
   sudo chmod 750 /home/ygg /home/ci
   ```
   - `--create-home --user-group`: a home directory and a group of the same name. `useradd` also gives the user
     its own range in `/etc/subuid` and `/etc/subgid`, which its rootless engine needs.
   - `--shell /bin/bash` for the users that log in over SSH (`ygg`, and `dev`); `/usr/sbin/nologin` for
     `ci`, `release` and `renovate`, which only run services.
   - `chmod 750`: closes each home to the other users. The users have no password, no `sudo` and are in no
     privileged group. If the admin user has the same name as one of them, it is left alone.
6. **The `ci` limits** (dev machine): `/etc/systemd/system/user-<uid>.slice.d/limits.conf` with six CPUs and
   20 GB (`CPUQuota=600%`, `MemoryMax=20G`), where `<uid>` is `id -u ci`, then `sudo systemctl daemon-reload`.
   **The low ports** (dev machine): for each of `ci`, `release`, `renovate` and `dev`,
   `/etc/systemd/system/user-<uid>.slice.d/50-yggdrasil-ports.conf` with `[Slice]`, `SocketBindDeny=tcp:1-1023` and
   `SocketBindDeny=udp:1-1023`, then `sudo systemctl daemon-reload`: only `ygg` may bind 80 and 443
   ([hosts.md](hosts.md#etcsystemdsystemuser-uidsliced50-yggdrasil-portsconf-dev-machine)).
7. **SSH.** First your key goes to the SSH users that have none:
   ```bash
   sudo install -d -m 700 -o ygg -g ygg /home/ygg/.ssh
   sudo install -m 600 -o ygg -g ygg ~/.ssh/authorized_keys /home/ygg/.ssh/authorized_keys
   ```
   - `install -d -m 700 -o ygg -g ygg`: creates `.ssh`, readable by `ygg` only.
   - `install -m 600 -o ygg -g ygg <from> <to>`: copies your keys to `ygg`, who owns them, readable by it
     alone. The installer asks first, and never replaces keys that are there.

   Then the drop-in `/etc/ssh/sshd_config.d/00-yggdrasil.conf` of hosts.md (no root login, no passwords,
   `AllowUsers` the admin and the role's SSH users), and:
   ```bash
   sudo sshd -t
   sudo sshd -T | grep -E '^(permitrootlogin|passwordauthentication|kbdinteractiveauthentication|allowusers) '
   sudo systemctl try-reload-or-restart ssh
   ```
   - `sshd -t`: the file's syntax is valid.
   - `sshd -T`: what SSH will really run with. The four lines must show `no`, `no`, `no` and your users. If
     another file sets one of them first (cloud images ship `50-cloud-init.conf`), the name `00-…` is
     what wins; if something still does, remove the drop-in again and find what sets it.
   - `try-reload-or-restart ssh`: applies it. Open a **new** SSH session to test before you close the old one.

   The installer does the checks itself and puts the file back if they fail.
8. **The checkout** goes to `ygg`: `sudo chown -R ygg:ygg /opt/yggdrasil && sudo chmod 755 /opt/yggdrasil`.
   `-R`: everything in it. `755`: others can read it, so `ci` and `release` can run the agent script.
9. **The secrets directories.**
   ```bash
   sudo install -d -m 750 -o ygg -g ygg /etc/yggdrasil
   sudo install -m 600 -o ygg -g ygg /opt/yggdrasil/env/platform.env.example /etc/yggdrasil/platform.env
   sudo install -m 600 -o ygg -g ygg /opt/yggdrasil/env/acme.env.example /etc/yggdrasil/acme.env
   sudo install -d -m 750 -o ci -g ci /etc/yggdrasil-ci
   sudo install -m 600 -o ci -g ci /opt/yggdrasil/env/agent.env.example /etc/yggdrasil-ci/agent.env
   ```
   - `/etc/yggdrasil` is `ygg`'s; every other service user has `/etc/yggdrasil-<user>` (`ci` and `release` get an
     `agent.env`, the others an empty directory). Those users have no login: edit their files as the admin with
     `sudoedit /etc/yggdrasil-ci/agent.env`, which keeps the owner and the `600`.
   - `-m 750`: the owner reads and writes, its group reads, nobody else gets in. Files are `600`: the owner only.
     They hold passwords and tokens.
   - A file that is already there is never replaced.
10. **The firewall.** Dev machine:
    ```bash
    sudo ufw allow OpenSSH && sudo ufw allow 80,443/tcp && sudo ufw allow 2222/tcp
    sudo ufw default deny incoming && sudo ufw default allow outgoing && sudo ufw enable
    ```
    Apps machine: SSH, and 80 and 443 only from Cloudflare's published ranges:
    ```bash
    sudo ufw allow OpenSSH
    sudo ufw default deny incoming && sudo ufw default allow outgoing
    { curl -fsS https://www.cloudflare.com/ips-v4; echo; curl -fsS https://www.cloudflare.com/ips-v6; echo; } | grep . > ~/cloudflare-ips
    for range in $(cat ~/cloudflare-ips); do
      sudo ufw allow proto tcp from "$range" to any port 80,443 comment yggdrasil-cloudflare
    done
    sudo install -m 644 -o ygg -g ygg ~/cloudflare-ips /etc/yggdrasil/cloudflare-ips
    sudo ufw enable
    ```
    - `ufw allow OpenSSH` comes first, so turning the firewall on does not cut your session.
    - `default deny incoming`: what no rule allows is refused (Ubuntu's default, but the rules mean nothing
      without it, so the installer sets it and `ygg check` reads it from `ufw status verbose`; the rules
      themselves it reads from plain `ufw status`, which names `OpenSSH` where verbose says `22/tcp (OpenSSH)`).
    - `2222/tcp`: Forgejo's git over SSH (the dev machine only).
    - `from "$range" … comment yggdrasil-cloudflare`: lets one Cloudflare range in on the two web ports and
      labels the rule, so that a later `ygg install` knows it is its own and can update it.
    - `| grep .`: drops the blank lines between the two lists, so the file is exactly one range per line,
      as the installer writes it (otherwise `ygg check` reports the file as different).
    - `cloudflare-ips`: the same ranges, which Traefik trusts for the visitor's address
      ([hosts.md](hosts.md#client-addresses)). Rootless Docker publishes ports as ordinary
      processes, so these rules apply to them: Docker no longer goes around `ufw`.
    - If Cloudflare's list cannot be downloaded, the installer still allows SSH, denies the rest and turns
      `ufw` on, and leaves only the Cloudflare rules and `cloudflare-ips` as they were (none, on a new machine:
      80 and 443 stay closed until `ygg install` runs again). It never opens 80 and 443 to everyone to make up
      for it.
    - After `cloudflare-ips` changes, Traefik trusts the new ranges once `scripts/platform.sh up` runs again as
      `ygg` (it renders Traefik's configuration from the file).
    - `ygg check` reports any rule that lets everyone in and that the installer did not make: another port, a
      range, or an application profile such as `Nginx Full` (80 and 443 under another name). On the apps machine
      the installer deletes plain `80`, `443` or `80,443/tcp` rules from anywhere; the others are yours to look at
      (`sudo ufw status numbered`, then `sudo ufw delete <number>`).
11. **Rootless Docker for each service user.** For `ygg` (and the same for each of the others):
    ```bash
    sudo loginctl enable-linger ygg
    ```
    then, **as that user** (`ssh ygg@<host>`; for a user that cannot log in, run each command through
    `sudo -u ci env XDG_RUNTIME_DIR=/run/user/$(id -u ci) DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u ci)/bus <command>`):
    ```bash
    mkdir -p ~/.config/systemd/user/docker.service.d
    nano ~/.config/systemd/user/docker.service.d/override.conf      # exactly the three lines of hosts.md
    dockerd-rootless-setuptool.sh install
    systemctl --user daemon-reload
    systemctl --user enable --now docker
    printf '\n%s\n%s\n%s\n' '# >>> yggdrasil >>>' "export DOCKER_HOST=unix:///run/user/$(id -u)/docker.sock" '# <<< yggdrasil <<<' >> ~/.profile
    ```
    - `loginctl enable-linger`: starts the user's systemd at boot and keeps it running with nobody logged in.
      Without it the engine stops with the user's last session.
    - `override.conf`: switches the engine's network and port driver to `slirp4netns`, so that Traefik sees
      the real address of each visitor. Its exact content is in
      [hosts.md, In each service user's home](hosts.md#in-each-service-users-home); the reason in
      [Rootless Docker](hosts.md#5-rootless-docker).
    - `dockerd-rootless-setuptool.sh install`: Docker's own tool; it makes the user's `docker.service`.
    - `systemctl --user enable --now docker`: starts the engine now and at every boot.
    - The `~/.profile` lines point the user's `docker` command at its own engine. The marker comments are
      what the installer looks for: it replaces the block between them on every run instead of adding another
      (the block's exact form is in [hosts.md](hosts.md#in-each-service-users-home)).
12. **The nightly backup**, as `ygg`:
    ```bash
    mkdir -p ~/.config/systemd/user
    cp /opt/yggdrasil/platform/systemd/yggdrasil-backup.service /opt/yggdrasil/platform/systemd/yggdrasil-backup.timer ~/.config/systemd/user/   # their content: hosts.md
    systemctl --user daemon-reload
    systemctl --user enable --now yggdrasil-backup.timer
    ```
    The timer fires at 03:30. It fails, loudly, until you write `backup.conf`: see
    [hosts.md, Backups](hosts.md#7-backups-and-restore).
13. **The role**: `echo dev | sudo tee /etc/yggdrasil-role` (or `apps`). Later `ygg check` and `ygg install`
    without `--role` use it.

When it finishes it prints the check again. Every row should be `ok`. The rows about the engines and the
isolation of `ci` and `release` need `sudo` without a password prompt; run `sudo -v` first so they are
checked.

### 8.4 The `ygg` command

```bash
scripts/ygg.sh self-install
```

This links `/usr/local/bin/ygg` to the checkout and installs tab completion, so `ygg` works from any
directory ([cli.md](cli.md#install)). Until then, `scripts/ygg.sh` works in its place in the steps below.

### 8.5 Check it

From your own computer, log in as `ygg` (the install copied your key) and test its engine:

```bash
ssh ygg@<host>
docker info --format '{{.SecurityOptions}}' && docker run --rm hello-world && docker compose version
python3 /opt/yggdrasil/scripts/catalog.py validate
```

- `docker info --format '{{.SecurityOptions}}'`: must list `name=rootless`. If it fails with
  `permission denied` or cannot connect, see [Troubleshooting](#troubleshooting).
- `docker run --rm hello-world`: runs a test container, which prints `Hello from Docker!`.
- `docker compose version`: prints the Compose plugin's version: v2.24 or later.
- `catalog.py validate`: reads the catalog and prints how many environments, systems and applications it has.

**From here, you work as `ygg`** (`ssh ygg@<host>`) for everything about the platform: the env files,
`platform.sh`, `ygg vars`, `ygg deploy`, `ygg backup`. You work as the admin (`ssh artur@<host>`) for
`sudo`, `ygg check`, `ygg install` and `ygg agent`.

### 8.6 Not on Ubuntu 24.04 or 26.04, or not behind Cloudflare

`ygg install --role dev|apps` refuses another system. A host like that has to do the same by hand: a
rootless Docker engine for the user that will run `platform.sh` (parts 1, 2, 3 and 11 above, with your
own user), `net.ipv4.ip_unprivileged_port_start=80`, and your own firewall. `platform.sh` needs only that
user's engine; it finds the socket from `DOCKER_HOST`, else `/run/user/<uid>/docker.sock`, and refuses a
rootful one.

## 9. The dev machine

Everything in this step, and in step 10, is done as `ygg` (`ssh ygg@<host>`), in `/opt/yggdrasil`. The
dev machine runs Traefik, the monitoring, **Forgejo** (profile `forge`) and the **Jenkins controller**
(profile `jenkins`). It is the controller host. The controller comes up in two passes: first the
controller alone, because each agent's secret only exists once the controller has created the agent;
then the agents. On the default two machines the agent that deploys runs on the apps machine
([step 10](#10-the-apps-machine)); the `ci` and `release` build agents on this machine are started with
`ygg agent` ([cli.md](cli.md#build-agents)). In a single-machine setup the deploy agent runs here, as
9.5 says.

### 9.1 The GitHub App key

Only with the GitHub App: a GitHub installation, or Forgejo with a `mirror`. Skip it on Forgejo without one.

Copy the `.pem` from [step 7.4](#74-generate-the-private-key) to the machine, to `ygg`. From your
computer, in the folder it was downloaded to (PowerShell on Windows works too):

```bash
scp <app>.private-key.pem ygg@<controller-host>:~/
```

- `<app>.private-key.pem`: the downloaded file's name, e.g. `acme-yggdrasil.2026-09-25.private-key.pem`.
- `ygg@<controller-host>`: the `ygg` user on the dev machine, e.g. `ygg@203.0.113.10`.
- `:~/`: puts it in `ygg`'s home directory.

Then, as `ygg` on the machine, in its home directory, convert it and delete the original:

```bash
cd ~
openssl pkcs8 -topk8 -inform PEM -outform PEM -nocrypt -in <app>.private-key.pem -out /etc/yggdrasil/github-app.pem
chmod 644 /etc/yggdrasil/github-app.pem
rm <app>.private-key.pem
```

- `openssl pkcs8 -topk8 -nocrypt`: rewrites the key as an unencrypted PKCS#8 key, the only format
  Jenkins' GitHub App credential accepts. GitHub's download is PKCS#1.
- `chmod 644`: the Jenkins controller runs as uid 1000 *inside its container*, which in a rootless
  engine is not `ygg` (that is container root) but an id from `ygg`'s subordinate range. It reads the
  file as "other", so "other" needs the read bit. Nobody else on the machine can reach it: the
  directory `/etc/yggdrasil` is `750`, owned by `ygg`.

### 9.2 The env files, first pass

`/etc/yggdrasil/acme.env`: your DNS provider's credential from step 6, e.g.
`CF_DNS_API_TOKEN=<token>` ([dns.md A5](dns.md#a5-configure-the-host) or
[B2](dns.md#b2-configure-the-host)).

`/etc/yggdrasil/platform.env`: every variable is documented in the file. Edit it with
`nano /etc/yggdrasil/platform.env` and fill in:

| Variable | Value |
|---|---|
| `ENVIRONMENTS` | **Empty** on a dev machine, which runs no environment (`platform.sh` refuses it without the `apps` profile). On a single machine: the ids of the environments it runs, comma-separated, no spaces, exactly as in `catalog.yaml`: `development,homologation,production` |
| `COMPOSE_PROFILES` | `forge,jenkins` (**not** `agent`). A single machine: `apps,jenkins` (**not** `apps,jenkins,agent` yet). On Forgejo, the controller reads three token files that must exist when it starts ([9.7](#97-the-forgejo-accounts-and-tokens)): start with `forge` alone, make the accounts (9.6 and 9.7), then add `jenkins` |
| `DOMAIN` | This machine's domain, e.g. `example.com`. `git.` and `jenkins.` are under it |
| `EXTRA_DOMAINS` | Empty on the dev machine |
| `CLOUDFLARE_PROXIED` | `false`: the dev machine's names are DNS only. On a single machine behind Cloudflare, `true` ([hosts.md](hosts.md#client-addresses)) |
| `PLATFORM_HOST_SUFFIX` | `-dev` on the dev machine: its Traefik dashboard and Grafana are then `traefik-dev.<DOMAIN>` and `grafana-dev.<DOMAIN>`, not the apps machine's `traefik.` and `grafana.`. They need DNS-only records to this machine ([step 6](#6-domain-and-dns), [hosts.md](hosts.md#dns-records)). Leave it empty on a single machine |
| `ACME_EMAIL` | Your e-mail address: the Let's Encrypt account's contact. Not an `@example.com` one |
| `ACME_DNS_PROVIDER` | `cloudflare`, `duckdns`, ... |
| `ACME_CA_SERVER` | **Uncomment** it (remove the `#`): the staging CA, for this first run |
| `TRAEFIK_DASHBOARD_USERS` | `admin:` and a password hash, in single quotes. See below |
| `GRAFANA_ADMIN_PASSWORD` | A password for Grafana's `admin` user |
| `YGGDRASIL_STATUS_TOKEN` | A random token, at least 32 characters. Compose wants it on every machine, even one without the status API; the console needs it where there is one: keep a copy in your password manager |
| `JENKINS_URL` | `https://jenkins.<DOMAIN>/` |
| `JENKINS_ADMIN_PASSWORD` | A password for the Jenkins `admin` user |
| `YGGDRASIL_LIBRARY_VERSION` | A release tag of your fork (`git tag --list 'v*'` in its clone), such as `v0.8.0`: the version of `jenkins/library/` that every pipeline runs. Required; `platform.sh up` stops without it. See [How a build runs](#how-a-build-runs) |
| `CI_AGENT_WORKDIR`, `RELEASE_AGENT_WORKDIR` | `/home/ci/agent` and `/home/release/agent`: the build agents' work directories, absolute, each exactly the `YGG_AGENT_WORKDIR` in that agent's `agent.env` ([9.8](#98-the-ci-and-release-agents)). Required |
| `GITHUB_APP_ID` | The App ID from [step 7.3](#73-note-the-app-id). Needed on GitHub, and on Forgejo with a `mirror` (GitHub Actions results and the releases' GitHub copy); leave it empty on Forgejo without one |
| `GITHUB_APP_KEY_FILE` | `/etc/yggdrasil/github-app.pem`, from 9.1; empty with `GITHUB_APP_ID` |
| `FORGE_WRITE_TOKEN_FILE`, `FORGE_READ_TOKEN_FILE`, `FORGE_WEBHOOK_SECRET_FILE` | Forgejo only, all three required: the files [9.7](#97-the-forgejo-accounts-and-tokens) makes (`/etc/yggdrasil/forge-write-token`, `forge-read-token` and `forge-webhook-secret` in the template) |
| `GITHUB_READ_APP_ID`, `GITHUB_READ_APP_KEY_FILE` | Optional, GitHub only, both or neither: a second GitHub App with read access only (contents, metadata, pull requests, commit statuses), installed on the same owner, and its PKCS#8 key (9.1's `openssl` and `chmod 644`). The CI agent then checks out with it, as it does with the `jenkins-ci` user on Forgejo. Without it the CI agent uses the main App, whose token can write: an accepted risk of a GitHub installation, and every build log says so |
| `NUGET_API_KEY_FILE` | Optional: a file with the nuget.org API key, for libraries whose tags push packages ([Owner tags and NuGet](#owner-tags-and-nuget)). Written with `printf '%s' <key> > <file>` and `chmod 644` for the reason given for the GitHub App key in 9.1 |

Use letters and digits only in passwords: `platform.sh` reads the file with bash, and Compose and
Jenkins read it too, so `$`, spaces, quotes or `;&()` break it. The secrets can be generated
straight into the file:

```bash
f=/etc/yggdrasil/platform.env
hash=$(htpasswd -nB admin)      # asks twice for the Traefik dashboard password you choose
sed -i "s|^TRAEFIK_DASHBOARD_USERS=.*|TRAEFIK_DASHBOARD_USERS='$hash'|" "$f"
sed -i "s|^GRAFANA_ADMIN_PASSWORD=.*|GRAFANA_ADMIN_PASSWORD=$(openssl rand -hex 16)|" "$f"
sed -i "s|^YGGDRASIL_STATUS_TOKEN=.*|YGGDRASIL_STATUS_TOKEN=$(openssl rand -hex 32)|" "$f"
sed -i "s|^JENKINS_ADMIN_PASSWORD=.*|JENKINS_ADMIN_PASSWORD=$(openssl rand -hex 16)|" "$f"
grep -E '^(GRAFANA_ADMIN_PASSWORD|YGGDRASIL_STATUS_TOKEN|JENKINS_ADMIN_PASSWORD)=' "$f"
```

- `htpasswd -nB admin`: prints `admin:<bcrypt hash>` for the user `admin`, without writing a file.
- `openssl rand -hex 16` / `-hex 32`: 32 / 64 random hexadecimal characters.
- `sed -i "s|^NAME=.*|NAME=value|" "$f"`: replaces the whole `NAME=` line of the file. `|` separates
  the parts because the hash contains `/`.
- The last line prints the generated passwords and token: save them in your password manager.

`platform.sh up` picks the files Jenkins reads for your setup (`casc.yaml`, then `casc.forgejo.yaml` or
`casc.github.yaml`, `casc.github-app.yaml` when the GitHub App is used, `casc.github-read.yaml` and
`casc.nuget.yaml` when their files are set) from the catalog's `forge` and `mirror` and these settings; you
choose none of them.

There is no Docker group or socket setting any more (`DOCKER_GID` is gone). `platform.sh` finds `ygg`'s
own engine and mounts its socket. Traefik, Alloy and the status API don't hold the socket: each one
reads Docker through a socket proxy of its own, which allows only the read-only requests that client
makes, and the proxies run as container root, which is `ygg` on the host.

Leave the agent section for 9.5, and the first Forgejo account for 9.6.

**With the variables store** ([variables.md](variables.md)), fill in both files as above, then
move them into the encrypted store, which becomes their only source (skip this to keep the files):

```bash
ygg vars init          # prints the key: store it in your password manager, then type saved
ygg vars import --all  # platform.env, acme.env and any application env files; renames them *.env.imported
```

Change a value later with `ygg vars set platform KEY=value` (or `vars edit platform`),
and delete the `*.env.imported` files once `platform.sh up` has worked. The check below works
either way. Check the settings:

```bash
/opt/yggdrasil/scripts/platform.sh config > /dev/null && echo "platform.env OK"
```

It prints `platform.env OK`, or names the missing variable (it also renders Traefik's configuration from the
settings, into a throwaway copy, and stops if they are wrong). Only `platform.sh up` writes the live one,
`/etc/yggdrasil/traefik.yml`, and recreates Traefik when its content has changed. That file stays: Traefik's
container mounts it at every start, a reboot included. Run `platform.sh` as `ygg`, never with `sudo`: it refuses
root, whose files there Traefik couldn't read. `platform.sh up` checks the rest (each of
`ENVIRONMENTS` against the catalog, the Jenkins variables) before starting anything.

### 9.3 Start it and check the certificate

```bash
/opt/yggdrasil/scripts/platform.sh up
```

The first start builds images and takes several minutes. It returns once the services are up, with a
table of them: on the dev machine `forgejo`, `jenkins` and `traefik` are `(healthy)` (on Forgejo, `jenkins`
only after [9.7](#97-the-forgejo-accounts-and-tokens) has added it to the profiles). Then follow [dns.md, Bring it up](dns.md#3-start-the-platform-against-the-staging-ca)
from step 3:

1. Read Traefik's errors with `docker logs yggdrasil-traefik-1 2>&1 | grep -iE 'acme|error' | tail -20`.
2. Check the staging certificate **from your own computer**: `curl -kvI https://yggdrasil.<DOMAIN> 2>&1 | grep -i issuer`
   shows an issuer with `(STAGING)`.
3. Fix any error now, against the staging CA, which isn't rate limited: see
   [dns.md troubleshooting](dns.md#troubleshooting).
4. Switch to the real certificate ([dns.md step 4](dns.md#4-switch-to-the-real-certificate)): put the
   `#` back in front of `ACME_CA_SERVER` in `platform.env`, then:
   ```bash
   /opt/yggdrasil/scripts/platform.sh down
   docker volume rm yggdrasil_letsencrypt
   /opt/yggdrasil/scripts/platform.sh up
   ```
   From your computer, `curl -I https://jenkins.<DOMAIN>/login` now works without `-k`, and
   `https://git.<DOMAIN>` shows Forgejo's sign-in page.

### 9.4 Jenkins and the webhook

1. Open `https://jenkins.<DOMAIN>` and sign in as `admin` with `JENKINS_ADMIN_PASSWORD` (on Forgejo, once
   [9.7](#97-the-forgejo-accounts-and-tokens) has started the controller). Jenkins has created, from the
   catalog:
   - one job per application and one per [`repositories`](catalog.md#repositories) entry, named after
     its catalog id. Every one runs the same Jenkinsfile ([How a build runs](#how-a-build-runs)); a
     repository without a `yggdrasil.yml` has nothing to check, so its builds say what they did not do
     and end green;
   - one agent per host it deploys to, named after the environments' `agent` (`vps` for the
     default VPS: **Manage Jenkins → Nodes**), and the two build agents, `ci` (4 executors) and
     `release` (1), all offline for now. The build agents come up in [9.8](#98-the-ci-and-release-agents);
   - the `actions-release` and `release-resume` jobs ([Finishing a release that
     stopped](#finishing-a-release-that-stopped)), and the `tag-check` job, which checks every day that each
     release's tag is still on the commit it was released from ([Version tags: tag-check and
     repairs](#version-tags-tag-check-and-repairs)).
2. **GitHub.** Back in the GitHub App's **General** page ([step 7.3](#73-note-the-app-id)), tick
   **Webhook → Active**, check the Webhook URL is `https://jenkins.<DOMAIN>/github-webhook/` and
   the events **Push**, **Pull request** and **Repository** are checked, and click **Save changes**.
   A GitHub App that a Forgejo installation uses only for its mirror needs no webhook.
3. **GitHub.** In the app's **Advanced** tab, **Recent Deliveries** lists what GitHub sent. If there's a
   `ping`, it should have a green check. If there's none yet, push any commit to an application
   repository's `develop`: its `push` delivery must get a green check and a `200` response. Use
   *Redeliver* after fixing anything.
4. **Forgejo.** The webhooks are made by `scripts/rules.py` in [step 12](#12-apply-the-branch-rules), one
   per repository, to `https://jenkins.<DOMAIN>/gitea-webhook/post`, with the secret of
   `FORGE_WEBHOOK_SECRET_FILE`. Jenkins' Gitea plugin verifies **no** signature (it has no field for the
   secret), so the secret proves nothing to Jenkins; a forged request can only start a scan, which reads
   Forgejo again with Jenkins' own read token. Forgejo refuses to deliver a webhook to a private address
   by default (`webhook.ALLOWED_HOST_LIST`); `platform/compose.yml` sets it to `external,jenkins`, so both
   the public `https://jenkins.<DOMAIN>` of step 12 (the default) and, over the Docker network of the dev
   machine, `http://jenkins:8080` (the controller's service name) are allowed as `FORGE_JENKINS_URL`;
   any other private address is still refused. Restart Forgejo (`platform.sh up`) after changing the setting.

### 9.5 The env files, second pass: this host's agent

Only for a single machine that runs the environments too. On two machines the deploy agent is on the
apps machine: go to 9.6, then step 10.

Under **Manage Jenkins → Nodes → \<this host's agent\>** (`vps`), copy the secret: the long
hexadecimal string after `-secret` in the connection command. Then, in `platform.env`:

| Variable | Value |
|---|---|
| `COMPOSE_PROFILES` | `apps,jenkins,agent` |
| `JENKINS_AGENT_NAME` | The agent's name: the `agent` this host's environments set in the catalog (`vps`), or the environment's id when it sets none |
| `JENKINS_AGENT_SECRET` | The secret |
| `JENKINS_AGENT_URL` | `http://jenkins:8080/`: on this host, the agent reaches the controller over the Docker network |

The agent drives `ygg`'s engine through its socket. It runs as container root, which is `ygg` on the host:
it can do what `ygg` can, and no more. Then:

```bash
/opt/yggdrasil/scripts/platform.sh up
```

The node now shows as connected in Jenkins.

One agent deploys to every environment of the host, with two executors; deploys of one application
never overlap. If this host's environments are all `trigger: manual` with no `agent`, Jenkins has
no agent for them: leave `COMPOSE_PROFILES=apps,jenkins`.

### 9.6 The first Forgejo account

Forgejo (profile `forge`) is at `https://git.<DOMAIN>`: registration is closed and nothing is visible
without signing in, so the first account is made from the command line, once, as `ygg`:

```bash
docker exec "$(docker ps -q --filter label=com.docker.compose.project=yggdrasil --filter label=com.docker.compose.service=forgejo)" \
  forgejo admin user create --admin --username <name> --email <you@example.com> --random-password
```

- `docker ps -q --filter label=…`: prints the id of the running Forgejo container. Compose puts these two
  labels on every container, so the command does not depend on its name, which varies with the Compose
  version.
- `docker exec <id> forgejo admin user create`: runs Forgejo's own command in that container.
- `--admin`: a site administrator. `--username`, `--email`: yours.
- `--random-password`: makes a password and prints it once (`generated random password is …`). Sign in with
  it at `https://git.<DOMAIN>` and change it.

Importing existing repositories into Forgejo, and the move of a whole installation from GitHub, are in
[cutover.md](cutover.md). The accounts Jenkins and Renovate use are next.

### 9.7 The Forgejo accounts and tokens

Only with `forge.type: forgejo`. Jenkins and Renovate act on Forgejo as accounts of their own, each with
the least it needs, and each token goes to one place. As `ygg` on the dev machine, with the container
found as in 9.6:

```bash
forgejo_user() {
  docker exec "$(docker ps -q --filter label=com.docker.compose.project=yggdrasil --filter label=com.docker.compose.service=forgejo)" \
    forgejo admin user "$@"
}
forgejo_user create --username jenkins --email jenkins@<DOMAIN> --random-password --must-change-password=false
forgejo_user generate-access-token --username jenkins --token-name jenkins --scopes write:repository,read:user,read:organization --raw
```

- `create … --random-password`: nobody signs in as these accounts, so the printed password is never used.
- `generate-access-token … --raw`: prints the token once. Do the same for each row of the table.

| Account | Access to every repository of the catalog, and to yggdrasil's | Token scopes | The token goes to |
|---|---|---|---|
| `forge.jenkinsUser` (`jenkins` by default) | **Write** collaborator ([Forgejo details](#forgejo-details-worth-knowing)) | `write:repository`, `read:user` and `read:organization` (`--scopes write:repository,read:user,read:organization`) | `FORGE_WRITE_TOKEN_FILE`, which becomes the Jenkins credential `forge-write` |
| `jenkins-ci` (the name is fixed) | **Read** collaborator | `read:repository`, `read:user` and `read:organization` (`--scopes read:repository,read:user,read:organization`) | `FORGE_READ_TOKEN_FILE`, the credential `forge-read` |
| `renovate` | **Write** collaborator | `write:repository`, `write:issue`, `read:user` and `read:organization` (`--scopes write:repository,write:issue,read:user,read:organization`) | `RENOVATE_TOKEN` of [9.9](#99-renovate) |

`forge-write` changes the git server: statuses, merges, tags, releases, uploads. Jenkins binds it on the
controller, to post statuses, and on the **publish agent** (`forge.publishAgent`: the deploy agent of the last
release environment, unless you set it) for the steps that merge, tag, release and upload. The `ci` and
`release` agents and the checks never hold it: they read with `forge-read`.

Both Jenkins tokens need `read:user` and `read:organization` besides their repository scope: when Jenkins scans a
repository, its Gitea plugin looks the owner up (`/api/v1/users/<owner>`, `/api/v1/orgs/<owner>`), and Forgejo
answers that only to a token with those scopes. Without them every scan fails with `GiteaHttpStatusException: HTTP
403/Forbidden` in the controller's log, the jobs find no branches and no pull requests, and no pull request ever gets
a status from Jenkins. To see what Forgejo says of a token file, as `ygg` and without printing the token:

```bash
for p in users/<owner> orgs/<owner>; do printf 'Authorization: token %s\n' "$(cat /etc/yggdrasil/forge-read-token)" | curl -s -H @- -o /dev/null -w "$p: %{http_code}\n" "https://git.<DOMAIN>/api/v1/$p"; done
```

Both lines should end in `200` (`orgs/<owner>` is `404` for a user rather than an organization, which is fine); a
`403` is a token without the scope. Run it for `forge-write-token` too. After writing a new token to the file,
restart the controller (`docker restart yggdrasil-jenkins-1`) and **Scan Repository Now** on the jobs.

The live suite proves the rules
with a `write:repository` token ([setup step 12](#12-apply-the-branch-rules)) but gives the Jenkins user all
scopes; if Forgejo refuses a call of the pipeline with the narrower token, widen it.

Then the three files the controller reads, as `ygg`, with no trailing newline (a newline would become part of the
secret):

```bash
read -rs -p 'jenkins token: ' token && printf '%s' "$token" > /etc/yggdrasil/forge-write-token; echo
read -rs -p 'jenkins-ci token: ' token && printf '%s' "$token" > /etc/yggdrasil/forge-read-token; echo
openssl rand -hex 32 | tr -d '\n' > /etc/yggdrasil/forge-webhook-secret
chmod 644 /etc/yggdrasil/forge-write-token /etc/yggdrasil/forge-read-token /etc/yggdrasil/forge-webhook-secret
```

- `read -rs`: reads the token without showing it or keeping it in your shell history.
- `chmod 644`: for the reason given for the GitHub App key in 9.1. The controller runs as uid 1000 inside its
  container, which is not `ygg` but a subordinate id, so it reads the files as "other". The directory
  `/etc/yggdrasil` (`750`) keeps everyone else out.
- The webhook secret is a random value: `rules.py` puts it on every webhook in step 12, so read it back there
  (`cat /etc/yggdrasil/forge-webhook-secret`).

Give `jenkins` **write** access to each repository, and `jenkins-ci` **read** access, as collaborators (the
repository's **Settings → Collaborators**). Nothing is visible signed out, so a repository `jenkins-ci` cannot
see (a private one it was not given) is a repository Jenkins cannot scan or check out. When all of this exists,
add `jenkins` to `COMPOSE_PROFILES` and run `platform.sh up` again (9.3).

Moving repositories from GitHub, `scripts/migrate.py collaborators` gives the bots their access instead, on
every catalog repository: `jenkins` and `renovate` write, `jenkins-ci` read, and a fourth account, **`claude`**,
write (the AI development user's; it needs no token yet). Make `claude` with `forgejo_user create` as above
before it runs ([cutover.md](cutover.md#23-forgejo-the-controller-and-the-agents)).

### 9.8 The CI and release agents

The dev machine runs the repositories' checks and builds on two Jenkins agents of their own, each in the
rootless engine of its own service user (`ygg install --role dev` made them, [hosts.md](hosts.md#3-users)):

| Agent | User | Executors | Runs |
|---|---|---|---|
| `ci` | `ci` | 4 | Every check, the branch policy and reading `yggdrasil.yml`. Capped at six CPUs and 20 GB |
| `release` | `release` | 1 | The release build, its validation, a library's packing and push of NuGet packages, and reading an owner's tag |

Neither holds a write credential; they check out with the read-only credential. Jenkins creates both nodes
when it starts. Once it is up (9.3 and 9.4), as the admin, for each of `ci` and `release`
([cli.md, Build agents](cli.md#build-agents) has the details):

```bash
sudoedit /etc/yggdrasil-ci/agent.env      # JENKINS_URL, JENKINS_AGENT_NAME=ci, JENKINS_AGENT_SECRET, YGG_AGENT_WORKDIR=/home/ci/agent
ygg agent up --user ci
sudoedit /etc/yggdrasil-release/agent.env # the same with release, /home/release/agent
ygg agent up --user release
```

The secret is on the node's page (**Manage Jenkins → Nodes → ci**). `YGG_AGENT_WORKDIR` must be the
`CI_AGENT_WORKDIR` (or `RELEASE_AGENT_WORKDIR`) of the controller's `platform.env`: a check's container is
started by the user's engine with the workspace's path, so a workspace anywhere else is an empty directory in
it. Both nodes should now show as connected.

`agent.env` may also set `TESTCONTAINERS_HOST_OVERRIDE`, the address a `docker: true` check's Testcontainers reaches
its containers at. Leave it out: `ygg agent up` uses the host's own primary IPv4 address, which is where the rootless
engines' published ports answer from a container, and prints it
([cli.md, Build agents](cli.md#build-agents)).

An agent started by an earlier release needs `ygg agent up --user ci` (and `release`; it runs `scripts/agent.sh up`) again: `build-agent.yml`
now hands the agent `YGG_DOCKER_SOCKET`, the host path of the user's socket, which a check that sets
`docker: true` needs, and `TESTCONTAINERS_HOST_OVERRIDE`, the host's address, where such a check reaches the
containers it starts. On the apps machine, `scripts/platform.sh up` recreates the deploy agent with
`YGG_DOMAIN`, the platform's `DOMAIN`, which `deploy.sh` puts under a site's host.

The `ci` and `release` engines keep every image a check pulled and every build cache it made. A weekly prune of
each, a systemd user timer that `ygg install` sets up, removes what the last week's builds did not use
([hosts.md, The CI engines: the weekly prune](hosts.md#the-ci-engines-the-weekly-prune)).

### 9.9 Renovate

Dev machine, Forgejo only. The `renovate` user (made by `ygg install`) runs Renovate once a day from a
systemd user timer, in its own engine: [hosts.md](hosts.md#renovate-the-daily-run) has the unit and what it
does. Make the Forgejo account `renovate` with the token of 9.7 (`write:repository`, `write:issue`, `read:user`
and `read:organization`) and write access to every repository, then as the admin:

```bash
sudoedit /etc/yggdrasil-renovate/renovate.env       # RENOVATE_TOKEN=<the token>; the rest can stay
cd /opt/yggdrasil && sudo -u renovate env XDG_RUNTIME_DIR=/run/user/$(id -u renovate) scripts/renovate.sh run
```

Optionally, put a GitHub token in the same file as `RENOVATE_GITHUB_COM_TOKEN=`: a fine-grained personal access
token with read-only access to public repositories and no permissions, so Renovate can read the release notes of
GitHub-hosted dependencies without GitHub's anonymous rate limit. `ygg check` warns while it is empty.

Renovate starts by asking Forgejo who it is (`GET /api/v1/user`), which Forgejo answers only to a token with
`read:user`: without it the run stops at `Initialization error`, `Authentication failure`. To see what Forgejo says
of the token in the file, without printing it:

```bash
sudo -u renovate bash -c 't=$(sed -n "s/^RENOVATE_TOKEN=//p" /etc/yggdrasil-renovate/renovate.env); printf "Authorization: token %s\n" "$t" | curl -s -H @- https://git.<DOMAIN>/api/v1/user; echo'
```

It prints the account (`"login":"renovate"`), or the scope it lacks.

Run it **once by hand first**, as above, and read what it says: the unit then runs it every day at 05:00. It
visits only the repositories of the catalog and, with `requireConfig: required`, skips a repository that has no
Renovate configuration of its own: there are no onboarding pull requests. Each repository's configuration comes
with the move of the repositories to Forgejo. Its branches are `renovate/*` and go into `develop`, where the
branch policy accepts them (a repository on `main` or `master` sets `baseBranchPatterns` in its own
`renovate.json` and gets them there); a build of a `renovate/*` branch never gets a credential. A Renovate pull
request that changes `yggdrasil.yml` (an image bump there) is a change to a protected file: it waits for the
owner's **Merge anyway** ([Forgejo details](#forgejo-details-worth-knowing)).

## 10. The apps machine

For each apps machine (and any other host that runs environments), prepared as in step 8 with
`--role apps`, once the controller is up (step 9). Everything here is done as `ygg`. In a
single-machine setup the machine is the controller host, so skip to the `ports` environments below.
This host's env files are simpler, because the controller is elsewhere. Each host has its own
variables store, if you use one: after filling in the files, `ygg vars init` and
`ygg vars import --all` there, as in [9.2](#92-the-env-files-first-pass).

- `acme.env`: the DNS credential, as in 9.2.
- `platform.env`:

  | Variable | Value |
  |---|---|
  | `ENVIRONMENTS` | This host's environment ids, comma-separated, e.g. `development,homologation,production` |
  | `COMPOSE_PROFILES` | `apps,agent` (**`apps`**: `ENVIRONMENTS` is refused without it) |
  | `DOMAIN` | **This** machine's domain, e.g. `example.com` |
  | `EXTRA_DOMAINS` | More domains it serves, e.g. `example.org` for the static sites. Each gets its own certificate |
  | `CLOUDFLARE_PROXIED` | `true`: the machine is behind Cloudflare's proxy. Needs `/etc/yggdrasil/cloudflare-ips`, which `ygg install --role apps` wrote |
  | `ACME_*`, `TRAEFIK_DASHBOARD_USERS`, `GRAFANA_ADMIN_PASSWORD` | As in 9.2, `ACME_CA_SERVER` uncommented for the first run |
  | `YGGDRASIL_STATUS_TOKEN` | A new `openssl rand -hex 32`: one token per host, covering all its environments |
  | `JENKINS_URL` | The **controller's** URL: `https://jenkins.<controller's DOMAIN>/` |
  | `JENKINS_AGENT_NAME`, `JENKINS_AGENT_SECRET` | From **Manage Jenkins → Nodes → \<this host's agent\>** on the controller |
  | `JENKINS_AGENT_URL` | **Empty**: the agent then dials `JENKINS_URL` |
  | `JENKINS_ADMIN_PASSWORD`, `GITHUB_APP_*` | Not needed: leave them empty |

Then, as on the controller host:

1. `/opt/yggdrasil/scripts/platform.sh config > /dev/null && echo "platform.env OK"`.
2. `/opt/yggdrasil/scripts/platform.sh up`.
3. Check the staging certificate from your computer:
   `curl -kvI https://yggdrasil.<this DOMAIN> 2>&1 | grep -i issuer` shows `(STAGING)`.
4. Put the `#` back in front of `ACME_CA_SERVER`, then
   `platform.sh down`, `docker volume rm yggdrasil_letsencrypt`, `platform.sh up`.

The agent dials the controller out over a WebSocket on 443, so this machine needs no inbound port for
Jenkins. Its node turns connected in Jenkins.

**`ports` environments** (`local`, on a developer's workstation) need no platform, agent or DNS: `deploy.sh` publishes
each application on the host: on `127.0.0.1` through `stacks/<app>.ports.yml`, or as the
application's own `docker-compose.yml` publishes it when it has one (often every interface). Set one up any time, from your fork's checkout, with the application
repositories cloned next to it:

```bash
export YGG_SECRETS_DIR=~/yggdrasil-env      # holds <environment>/<application>.env, or vars.db and vars.key
scripts/deploy.sh local shop-api ../shop-api dev
```

- `local`: the environment id. `shop-api`: the application id.
- `../shop-api`: a checkout of the application's repository.
- `dev`: the version to label the image with.

### Firewall

A rootless engine publishes ports as ordinary listening processes, so the `ufw` rules of step 8 apply to
them (rootful Docker went around `ufw`). In these stacks only Traefik publishes ports (80 and 443; and
Forgejo's 2222 on the dev machine): no other service has a `ports:` entry in `proxy` mode. Metrics, the
status API's internal port and Prometheus stay on the Docker networks. Keep it that way when writing stack
files.

## 11. Application env files

On each host, one set of variables per application and environment the host runs (twelve on the
default VPS: four applications in three environments): its configuration and secrets, plus what its
stack files need. Each environment has its own secrets, database and host names. They live in
**env files**, or in the host's [variables store](variables.md), where a value shared by several
applications or environments is defined once.

**In the variables store** (after `vars init`, [9.2](#92-the-env-files-first-pass)):

```bash
cd /opt/yggdrasil
ygg vars import heimdall-api@development ~/yggdrasil-apps/heimdall-api/docker/development.env.example
ygg vars set heimdall-api@development DB_PASSWORD=-       # typed hidden
ygg vars set @development DB_HOST=postgres.example.com    # shared by every application of development
ygg vars edit heimdall-api@development                    # or all of it in $EDITOR
```

`<application>@<environment>` is one application in one environment, `@<environment>` all of an
environment, `<application>` one application everywhere ([scopes](variables.md#scopes-and-layers)).
`ygg config <application> <environment>` and `add` use the store too. The rest of this
step describes what goes in; the files below are for a host without a store.

`ygg config <application> <environment>` creates a missing file from the stack files and
opens it ([cli.md](cli.md#on-a-machine-without-a-store)). By hand:

```bash
install -d -m 750 /etc/yggdrasil/<environment>
nano /etc/yggdrasil/<environment>/<application>.env
chmod 600 /etc/yggdrasil/<environment>/<application>.env
```

- Run as `ygg`, who owns `/etc/yggdrasil`.
- `<environment>`: one of this host's environment ids, e.g. `development`. `<application>`: the
  application's id, e.g. `shop-api`.
- `install -d -m 750`: creates the environment's directory, for `ygg` alone.
- `chmod 600`: only `ygg` reads and writes the file. The deploy agent reads it as container root, which
  is `ygg` on the host, so it needs no group.

What goes in it:

- Start from the application repository's own env template, if it has one.
- Add what the stack files read. The overlays in this repository use:
  - `PUBLIC_HOST`: the application's host name, `<host><hostSuffix>.<DOMAIN>`, matching its
    catalog `host` and the environment's `hostSuffix` (`shop-api-dev.example.com` in development,
    `shop-api.example.com` in production);
  - `UI_HOST`: for a web UI, or an API served under its UI's origin.
- A web UI's build arguments (an API base URL, say) are compiled into its bundle: public, and
  different per environment. `deploy.sh` builds the image once per environment for that reason.
- An application calling another reaches the one **of its own environment**: on the `edge` network
  at `<id>.<environment>` (`http://shop-api.development:8080`), or at its public host name
  when it needs HTTPS. The plain `<id>` alias no longer exists.

`deploy.sh` passes this file (or, with the store, the same variables rendered from it) to Compose (`--env-file`): it fills in the `${VAR}` references of the
Compose files. A variable reaches the application's container only where its `docker-compose.yml`
or `stacks/<id>.yml` names it, in `environment:` (`DB_PASSWORD: ${DB_PASSWORD:?}`) or with
`env_file:`.

`python3 /opt/yggdrasil/scripts/catalog.py environments <application-id>` lists the environments an
application deploys to. [examples/docker-desktop-and-vps/env](examples/docker-desktop-and-vps/env)
has examples for four applications in the four default environments.

## 12. Apply the branch rules

On your machine, in your fork. One script applies the branching model to every repository of the catalog
(the applications' repositories and the [`repositories`](catalog.md#repositories) entries), on whichever git
server the catalog's [`forge.type`](catalog.md#git-server) names; no `forge` block means GitHub:

```bash
python3 scripts/rules.py --dry-run     # what would change, per repository
python3 scripts/rules.py               # apply
```

- On an **existing GitHub installation**, read [An existing GitHub installation](#an-existing-github-installation)
  first: run it for a repository only once Jenkins reports that repository's checks.
- **Moving from GitHub to Forgejo**, the script runs in the maintenance window, after the repositories are imported,
  their GitHub copies prepared, the push mirrors made and the bots given their access:
  [cutover.md, 5.15](cutover.md#515-the-rules).
- `--dry-run` reads from the server and prints what it would write, without writing anything. It still needs
  the token below.
- Without it, the script applies the rules. It is safe to run again: on Forgejo it writes only what differs
  from what the server already has (and prints `unchanged` for the rest); on GitHub it updates the rulesets in
  place, by name. On Forgejo it never deletes anything. On GitHub it deletes one thing: a ruleset named
  `default` (the ad-hoc protection of the default branch from before `github/rulesets.py`, which would
  otherwise go on protecting `develop` with different rules), so keep that name for nothing of your own.
  Otherwise it never deletes a protection: one the model no longer has (the `release/*` rule and the
  tag protection of a repository turned `rules: basic`, a tag protection whose pattern the catalog changed;
  on GitHub a `release tags` or `release tag creation` ruleset) is left as it is, with a `note:` line; delete it
  by hand if it is stale. A GitHub copy's `mirror tags` is not looked at once its repository is `rules: basic`:
  delete it by hand when you turn one basic.
- Name repositories to apply to some only: `python3 scripts/rules.py shop-api shop-web` (the repository
  names: an application's `repository`, or its `id`, or a `repositories` entry's `id`).
- `python3 scripts/rules.py --help` lists the variables. `github/rulesets.py` still works, but it only
  prints that it moved and runs this script with the same arguments.

What the script needs, by server:

| Server | Needs |
|---|---|
| **Forgejo** (`forge.type: forgejo`) | `FORGE_TOKEN`: the owner's token, with the `write:repository` scope and no other (the live test suite runs the script with a token of that scope). Create it in Forgejo, signed in as the owner: **Settings → Applications → Generate New Token**. `FORGE_JENKINS_URL` and `FORGE_WEBHOOK_SECRET`, together, to create the webhooks that tell Jenkins about pushes, tags and pull requests; with neither, the script says `webhooks skipped` and sets no webhook; with only one, it refuses to start. (It does not read `JENKINS_URL`, which Jenkins sets in every build.) |
| **GitHub** (no `forge` block, or `github`) | An authenticated `gh` (`gh auth login`), or `GH_TOKEN`, with admin rights on the repositories. Rulesets on **private** repositories need a paid GitHub plan. |
| **Forgejo with a [`mirror`](catalog.md#git-server)** | Everything Forgejo needs, and a GitHub token for the copies (`<mirror.github>/<repository>`): an authenticated `gh`, or `GH_TOKEN`, with **Administration: read and write** on them (a fine-grained token limited to those repositories will do). Each copy gets one ruleset, `mirror tags`, and nothing else ([Version tags](#version-tags-tag-check-and-repairs)). Without a token the script still applies the Forgejo rules, says `mirror tags not applied`, and exits `1` at the end (`the mirror tags were not applied to the GitHub copies ...`): run it again with one (`gh auth token` is tried when `GH_TOKEN` is not set, so being logged in to `gh` is enough). A copy GitHub refuses is skipped with a line `<repository> (GitHub copy <owner>/<repository>): not applied: <error>`, the other repositories are still applied, and the run exits `1` at the end naming the copies. The two usual causes: **`404 Not Found`**, the push mirror has not created that copy yet (set up the repository's push mirror, let it sync, run again); **`403`**, a **private** copy on a free GitHub plan, which has no rulesets for private repositories (a paid plan, or a public copy, fixes it; until then that copy's tags are unprotected and the run stays red). A `rules: basic` repository's copy is never asked. |

On Forgejo, set the variables for the one command, so they stay out of your shell history files and your
environment afterwards:

```bash
read -rs FORGE_TOKEN && export FORGE_TOKEN          # paste the owner's token, then Enter
read -rs FORGE_WEBHOOK_SECRET && export FORGE_WEBHOOK_SECRET
export FORGE_JENKINS_URL=https://jenkins.<DOMAIN>   # the controller's address (step 6)
python3 scripts/rules.py --dry-run
```

- `read -rs <name>`: reads a line without showing it (`-s`) and without treating `\` specially (`-r`).
- `export`: makes the variable visible to the script. The tokens and the secret are never printed by the
  script, and never put on a command line.
- `FORGE_WEBHOOK_SECRET` is the secret in Jenkins' `forge-webhook-secret` file, which [9.7](#97-the-forgejo-accounts-and-tokens)
  made (`cat /etc/yggdrasil/forge-webhook-secret`); [hosts.md](hosts.md#6-credentials) says where else it lives.
  Each repository gets one hook, `<FORGE_JENKINS_URL>/gitea-webhook/post` (JSON, with that secret). Jenkins'
  Gitea plugin does not verify it ([9.4](#94-jenkins-and-the-webhook)).

### What each server gets

Per repository, from the catalog (a `rules: basic` repository gets `develop`, `main` and `master` without force
push or deletion and nothing else of the table -- on Forgejo three branch protections, on GitHub three rulesets of
those names; `master` is a fork's default branch, which the basic model treats like `main` -- plus the settings in
its first row but its default branch, which stays as it was, and, on Forgejo, the webhook). A repository whose deciding entry is an application with no `trigger:
release` environment, such as a site deployed on pushes to `main`, is basic too: under the release model only
`forge.jenkinsUser` merges its `main`, and Jenkins merges only for a release ([One repository, several
entries](catalog.md#one-repository-several-entries)).

| Rule | Forgejo | GitHub |
|---|---|---|
| Default branch, merged branches | `develop` is the default (a basic repository keeps its own); head branches are deleted after merge; merge commits and squashes allowed, rebasing not | `develop` is the default (not for a basic repository); head branches are deleted after merge |
| `develop` | A protected branch: no direct push (force push and deletion are always refused), changes only through a pull request whose required checks pass: `branch-policy`, the application's `checks` and its `actionsChecks`. Merged by the owner and `forge.developers` | A `develop` ruleset: no deletion or force push, a pull request (squash or merge commit), the same required checks. `actionsChecks` are bound to GitHub Actions, so only that app can satisfy them; the other checks (Jenkins') are bound to nobody |
| `main` | The same, with `branch-policy`, `checks` and one `deploy/<environment>` per release environment. Merged only by `forge.jenkinsUser` for an application, only by the owner for an `owner-tag` repository | A `main` ruleset: the same required checks, all bound to nobody (`actionsChecks` are not required on `main`), merge commit only. Who merges is not restricted: the checks are the gate |
| `release/*` | Only the owner pushes | No rule (none today) |
| Version tags | A tag protection: `v*` can be created only by `forge.jenkinsUser` for an application; for an `owner-tag` repository, its `tags` pattern only by the owner. Nobody else can create, move or delete them (in an application's repository not even the owner). **Accepted gap:** a Forgejo tag protection is one list of who may change a matching tag in any way, so the user who creates the tags can also **move** them (a forced push) and **delete** them (a push; through the API too, unless the tag has a release): `forge.jenkinsUser` an application's `v*` tags, the owner an `owner-tag` repository's. A release whose tag was deleted becomes a draft (a moment after the push), naming a tag that is gone; pushing the tag back publishes it again. The live tests (`scripts/test_forgejo_live.py`) pin this. The `tag-check` job detects it, and with a `mirror` each repository's GitHub copy gets a `mirror tags` ruleset that keeps its tags as released: [Version tags: tag-check and repairs](#version-tags-tag-check-and-repairs) | A `release tags` ruleset: tags cannot be moved or deleted, by **anyone** (it has no bypass: the owner neither). For an `owner-tag` repository a second ruleset, `release tag creation`: only repository administrators (the owner) create its tags |
| The owner | Not bound by the protections, for emergencies (see below) | A repository administrator bypasses all the rulesets but `release tags` |
| `yggdrasil.yml` (and `Jenkinsfile`) | **Protected files** on `develop` only: a pull request that changes one can be merged only by the owner, with a forced merge (below); being on the list of who may merge is not enough (`forge.developers` included). `yggdrasil.yml` says what Jenkins runs for the repository (images, commands, which allowed credential a check binds), so every change to it reaches `develop` through the owner, **Renovate's too**: a `renovate/*` pull request that bumps an image in `yggdrasil.yml` passes its checks and then waits for the owner's **Merge anyway**. `main` has no protected file: it receives only snapshots of `develop`, merged by `forge.jenkinsUser`, which a release that changes `yggdrasil.yml` must not stop. Jenkins no longer reads a repository's `Jenkinsfile` ([why](#how-a-build-runs)); it stays on the list, harmlessly, for repositories that still carry one | Not applied: GitHub cannot restrict who may merge a given file. On GitHub a change to `yggdrasil.yml` is guarded only by who may merge into `develop` |

### An existing GitHub installation

**Before this release reaches your fork's `main`, pin the controller's library to your current release.** A
controller set up before it loads the library at `defaultVersion: "main"`, so the moment `main` has this release
it would run the new library, before `scripts/platform.sh up` has given it what that needs: builds would wait for
a `ci` agent that does not exist, and the HTTP Request plugin it posts statuses with is not installed. In
Jenkins: **Manage Jenkins → System → Global Trusted Pipeline Libraries** (Global Pipeline Libraries on older
versions) → `yggdrasil` → **Default version**: your current release tag (`git tag --list 'v*'` in your fork,
e.g. `v0.7.4`) → **Save**. Keep the controller host's checkout at that release too until you upgrade: a restart
re-applies its `casc.yaml`, which says `main`. Pinned, the old library keeps working while `main` moves on. It
still clones `scripts/` from `main` for every build, and what it calls there stays compatible:
`github.sh wait-checks`, `set-status`, `merge-pr`, `release`, `publish-release` and `delete-branch` take the
same arguments and print the same output (they now go through `scripts/forge.sh`), and `deploy.sh <environment>
<application> <checkout> <version>` is unchanged (its new `YGG_DOMAIN` and `DEPLOY_INPUTS_DIR` are optional).
The pin is replaced by `YGGDRASIL_LIBRARY_VERSION` when you upgrade with `scripts/platform.sh up`.

**Run a scratch repository through it on GitHub before you rely on it.** The pipeline's GitHub paths (the
GitHub branch source with the library at a tag, `github-read`, GitHub Actions results, release pull requests,
releases and their files) have been tested against fakes only, never against GitHub itself; Forgejo's ran live.
Before you merge this release into `main` for an installation that runs on GitHub (or set one up on GitHub),
take a throwaway repository on GitHub with a `yggdrasil.yml` and a catalog entry with `forge.type: github` (no
`forge` block is the same) and a release environment, and run it end to end: a pull request into `develop`
(branch policy and checks reported), a push that deploys, and a release pull request into `main` that deploys,
merges, tags and publishes its release (with files, if your repositories have any). Only then move your own
repositories.

An installation that ran on GitHub before the pipeline moved into Jenkins keeps working without a change to
any repository: its repositories have no `yggdrasil.yml`, so they deploy and release as they always did (a
release waits for every GitHub Actions check, and `releaseArtifacts: true` still runs its workflow). Two
things are different: Jenkins never reads their `Jenkinsfile` any more ([why](#how-a-build-runs)), and every
build now starts with a short step on the **CI agent** that reads the commit (and finds no `yggdrasil.yml`),
so the CI agent ([9.8](#98-the-ci-and-release-agents)), `CI_AGENT_WORKDIR`/`RELEASE_AGENT_WORKDIR` and
`YGGDRASIL_LIBRARY_VERSION` ([9.2](#92-the-env-files-first-pass)) are needed before the first build.

`scripts/rules.py` does not apply quite what `github/rulesets.py` did. That one bound every required check
to GitHub Actions; this one binds only the `actionsChecks`, because Jenkins reports `branch-policy` and the
`checks`. Run on a GitHub installation, it changes the rulesets so:

- on `develop` and `main`, `branch-policy` and the `checks` are still required but **bound to nobody**: a
  status of that name from anyone who may post statuses satisfies them, not only GitHub Actions' job;
- the `actionsChecks` are required, bound to GitHub Actions, **on `develop` only**: a GitHub Actions job
  moved from `checks` to `actionsChecks` is no longer required on `main` at all (Jenkins waits for them
  before a release instead);
- `deploy/<environment>` on `main` is unchanged (it was never bound).

That is right once Jenkins reports those contexts, which it does for a repository with a `yggdrasil.yml`. So
**adopt the file in a repository first, then run `rules.py` for it**:

1. **Move each Actions job's steps into `ci/` scripts** (`ci/test.sh`, ...) that run on a clean machine with
   only the job's image: the same steps, from the repository's root, which you can also run by hand.
2. **Write `yggdrasil.yml`** with a check for each of those jobs: `image` (the job's container or the tool's
   image), `run: ci/test.sh`, and what it needs: `docker: true` for Testcontainers, `credentials` (Jenkins
   credential ids the catalog allows this repository), `junit`, `archive`
   ([catalog.md](catalog.md#repository-pipeline-file)). Validate it where the catalog is:
   `python3 scripts/pipeline_config.py describe <application-id> <repository-checkout>`.
3. **Update the catalog**: the Jenkins checks go in the application's `checks` (their names in the file), and
   the jobs that must stay in GitHub Actions, **Windows and macOS** ones, go in `actionsChecks`, each by its
   job name. Add `credentials` for the ids the checks bind. To have Jenkins reuse a commit's results, set
   `forge.jenkinsUser` to the App's bot login (`<app-slug>[bot]`); otherwise every check runs again.
4. **Push to `develop` and open a pull request**: Jenkins reports `branch-policy` and every check as statuses
   on it. Delete the Actions jobs you moved, and the Branch Policy workflow, so that Jenkins is the only one
   that reports those names; delete the repository's `Jenkinsfile` too.
5. **Now run `python3 scripts/rules.py --dry-run <repository>`, then without `--dry-run`**: the required
   checks are the ones Jenkins reports.

Repositories you have not moved keep their rulesets and their Actions jobs until their turn: run `rules.py`
for the others only after they adopt the file.

### Forgejo details worth knowing

- **Write access first.** A user named in a rule (`forge.jenkinsUser`, the `forge.developers`) is kept on
  Forgejo's lists of who may merge, push or create tags only while they have **write access to the
  repository**. Otherwise Forgejo leaves them off *without an error*. Add each of them as a collaborator with
  write access (the repository's **Settings → Collaborators**) before the first run. If one is missing, the
  script prints `warning: Forgejo left <user> off who may merge|push|create tags (…): give them write access to
  the repository and run again`; do that and run it again.
- **The owner's override is a forced merge.** The protections do not bind the owner (an administrator), for
  emergencies, but a plain merge by the owner is still refused while a required check fails, and also for a
  pull request that changes a protected file (`yggdrasil.yml` or the `Jenkinsfile` on `develop`: a
  Renovate pull request that bumps an image in `yggdrasil.yml` included). In the web
  interface use **Merge anyway** on the pull request; through the API, `force_merge: true` on the merge
  call. Nobody else can.
- **Changing the webhook secret.** Forgejo never returns a hook's secret, so the script cannot tell that
  `FORGE_WEBHOOK_SECRET` changed. The hook is rewritten only when its address, events, content type, branch
  filter or active flag differ. To use a new secret, delete the repository's hook (**Settings → Webhooks**)
  and run the script again; it creates the hook with the new secret. (The same secret goes into Jenkins.)
- **Users must exist.** The `jenkins` user, and any `forge.developers`, are created by you; the script does
  not create accounts or collaborators.

Run the script again after adding an application or a repository to the catalog (the full steps are in
[Adding things](#adding-things)) and after adding a release environment: `main` then also requires its
`deploy/<environment>`. From then on `develop` and `main` change only through pull requests. (On an existing
GitHub installation, run it for a repository that has adopted `yggdrasil.yml`; for the others add a new
`deploy/<environment>` by hand: [An existing GitHub installation](#an-existing-github-installation) says why.)

### Version tags: tag-check and repairs

A version tag names the commit that was released, for good. Who may move or delete one:

| Where | Moves or deletes a version tag |
|---|---|
| Forgejo, an application's `v*` | `forge.jenkinsUser`, the user that creates them (the accepted gap: one Forgejo list covers creating, moving and deleting) |
| Forgejo, an `owner-tag` repository's `tags` | The owner, who creates them (the same gap) |
| A GitHub installation: `release tags` ruleset | Nobody, the owner included: it has no bypass. An `owner-tag` repository's tags are *created* only by administrators, through the separate `release tag creation` ruleset, whose bypass covers creating and nothing else |
| A Forgejo installation's GitHub copies (with a `mirror`): `mirror tags` ruleset | Nobody, the owner included. The mirror creates the tags there (no creation rule) and keeps pushing the branches (no branch rule, no setting changed) |

The accepted gap means a mistake, or a stolen token of `forge.jenkinsUser`, can move or delete a released tag
on Forgejo. Two things answer it:

- **The `tag-check` job notices.** Every day (at a time Jenkins picks within the 4 o'clock hour), and whenever
  you run it (**Build with Parameters**, then **Build**), it reads every catalog entry that releases (an application with a
  `trigger: release` environment, an `owner-tag` entry) on the `ci` agent with the read credential: each
  published release (`forge.sh release-tags`) and where its tag is now (`forge.sh tag-sha`). It checks out and
  runs nothing of the repositories and holds no write token. A deleted tag's release does not stay published:
  Forgejo (a moment after the push) and GitHub turn it into a draft, which the read credential cannot see. So
  each run archives the releases it knows of (`release-tags.tsv`, an artifact of the build) and the next run
  checks those too, published or not: a release the job has seen once is checked on every run after. One whose
  tag was deleted before the job's first run is not noticed, so **run it once by hand as soon as the job
  exists** (after the upgrade's `platform.sh up`, or [9.4](#94-jenkins-and-the-webhook) on a new installation)
  to start the record, rather than waiting for the night.
- **The GitHub copies keep the tags as they were released.** With a `mirror`, `rules.py` puts `mirror tags` on
  every copy, so the push mirror cannot move or delete a version tag there: GitHub refuses a mirror push that
  would, and Forgejo shows that repository's push mirror failing to sync (its **Settings → Repository**, the
  push mirror's last update) until the tag is repaired. That is intended: the copy is the record to repair
  from.

**Reading tag-check.** Green means every release's tag is on the commit it was released from; the build's
description says how many were checked (`17 release tags checked`). Red means at least one is not; the
description counts them (`2 release tags moved or deleted`), and the last lines of the log name each one:

| Line | Means |
|---|---|
| `MOVED: <repository> <tag> (tag on <sha>, released from <sha>)` | The tag now points at another commit. The second `<sha>` is the one to put it back on |
| `DELETED: <repository> <tag> (released from <sha>): the tag is gone` | The release is there, its tag is not |
| `NOT CHECKED: <repository> ...` | The job could not read that repository or tag (`forge.sh`'s message is above it). It is red too, since nothing was proved; usually the read credential lost access ([9.7](#97-the-forgejo-accounts-and-tokens)) |

A line `<repository> <tag>: unknown target: released from '<name>', not a commit ...` earlier in the log is not
a failure: that release names a branch instead of a commit (one made by hand, or from before the pipeline made
releases), so whether its tag moved cannot be told. A deleted tag is reported `DELETED` either way. Nor is
`... ok (<sha>), but its release is not published (a draft, or deleted) ...`: the tag is fine, the release
was unpublished or deleted (publish it again if that was not meant).

The record keeps a release whose tag you **deleted on purpose** red on every run. **Repair a deletion that was
not meant first** (below); then, for one that was, run the job with **`FORGET`**: one `<repository> <tag>` a
line (`shop-api v1.4.0`), the repository as in the `DELETED` line. Each line must name a repository the job
checks and a tag name the pipeline accepts, or the build fails before it checks anything. The run drops those
releases from the record it keeps (`Forgotten: <repository> <tag> ...` in the log) and checks the rest as
usual; a line that names nothing in the record only says so (`... is not in the record: nothing to forget`).
The later runs, `FORGET` empty, start from that record. A forgotten release whose release is published again is
checked again.

**Repairing a moved or deleted tag on Forgejo**, as the owner, on your machine:

1. Get the tag from the GitHub copy into a clone of the Forgejo repository, and check it is the commit
   tag-check says it was released from:

   ```bash
   git clone https://git.<DOMAIN>/<owner>/<repository>.git && cd <repository>
   git fetch --no-tags https://github.com/<mirror.github>/<repository>.git "+refs/tags/<tag>:refs/tags/<tag>"
   git rev-parse "<tag>^{commit}"      # must print the "released from" commit of tag-check's line
   ```

   The `+` lets the fetch replace the moved tag the clone came with, and an annotated tag keeps its message. A
   private copy needs your GitHub credentials for git (`gh auth setup-git` sets them up). Without a mirror, put
   the tag on the commit tag-check named instead: `git tag --force <tag> <sha>` (Jenkins' `v*` tags are
   lightweight, so nothing is lost; an annotated tag of yours needs `git tag --annotate --force -m <message> <tag>
   <sha>`).
2. Push it back:
   - **An application's `v*` tag.** Only `forge.jenkinsUser` may push `v*` tags, so let yourself in for the
     moment: in Forgejo, the repository's **Settings → Tags**, edit the `v*` protection and add yourself to who
     may push it (you are the repository's administrator). Then, in the clone, and in your fork for `rules.py`:

     ```bash
     git push --force origin "refs/tags/<tag>"
     python3 scripts/rules.py <repository>     # puts the protection back to forge.jenkinsUser alone
     ```

     `rules.py` needs `FORGE_TOKEN` as above (and, with a mirror, a GitHub token); it prints `updated tag
     protection 'v*'`.
   - **An `owner-tag` repository's tag.** You may push it already: `git push --force origin "refs/tags/<tag>"`.
     If the tag is less than a day old (an annotated tag is dated when it was made, a lightweight one by its
     commit), Jenkins sees it as a new tag and **builds it again** ([Owner tags and NuGet](#owner-tags-and-nuget)):
     the release is redone from the tag the way `release-resume` does it, reusing the release and the NuGet
     versions already there but rebuilding the files (the cautions of [Finishing a release that
     stopped](#finishing-a-release-that-stopped) apply). Abort that build in Jenkins if you don't want it.
3. Run tag-check again (**Build with Parameters**, `FORGET` empty): it is green once every tag is back, and the push mirror's next sync
   goes through again. On Forgejo pushing a deleted tag back also publishes its release again (the live tests
   pin it).

On a **GitHub installation** a version tag can be deleted only after its ruleset was disabled (below). If one
was, push it back the same way while the ruleset is still disabled, then check its release on GitHub: GitHub
leaves a deleted tag's release a draft, so publish it by hand if it still is one, and run `rules.py` to turn the
ruleset back on.

**A wrong tag on GitHub.** Nobody bypasses `release tags` or `mirror tags`, so to fix a tag on GitHub (one
created on the wrong commit, say), edit the ruleset first: the repository's **Settings → Rules → Rulesets**,
open it, set **Enforcement status** to **Disabled** and save; fix the tag; then run `python3 scripts/rules.py
<repository>`, which turns it back on (it writes every ruleset whole, `active` included).

### The git server commands

The pipeline and the scripts never talk to the git server themselves: they call `scripts/forge.sh`, one set of
commands that does the same on Forgejo and on GitHub (`scripts/github.sh` keeps only what is GitHub Actions
and hands the rest to it). You rarely run it by hand, but it is how to check a token or finish a release
that stopped halfway. Every command takes the **repository name** (not `owner/name`; the owner comes from the
catalog):

| Command | Does | Prints |
|---|---|---|
| `set-status <repo> <sha> <state> <context> <description> [target-url]` | Sets a commit status; `state` is `pending`, `success`, `failure` or `error` | nothing |
| `get-statuses <repo> <sha>` | Reads the commit's statuses | `<context>`, a tab, `<state>`, a tab, `<creator>`, one line per context (the latest); `<creator>` is the login that set the status, empty when the server has none. The pipeline reuses a result only when its creator is `forge.jenkinsUser` |
| `merge-pr <repo> <number> <sha> <title>` | Merges a pull request with a merge commit, and fails if its head is no longer `<sha>` | the merge commit |
| `create-tag <repo> <tag> <sha>` | Creates a tag | nothing |
| `create-release <repo> <tag> <sha> <title> [--draft] [--notes-file <file>]` | Creates a release (an unpublished draft with `--draft`), a **prerelease** when the tag's version has a SemVer pre-release part ([Releases](#releases)) | the release id, a tab, its page address |
| `upload-release-file <repo> <release-id> <file>` | Attaches a file to a release | the file's download address (on Forgejo `<server>/attachments/<uuid>`) |
| `publish-release <repo> <release-id>` | Publishes a draft release | the release's page address |
| `release-tags <repo>` | Lists the published releases (drafts left out) | `<tag>`, a tab, `<target>` (the commit it was released from: the pipeline sends the release's commit; a release made by hand may name a branch), one line per release |
| `delete-release-file <repo> <release-id> <name>` | Deletes a release's file by its name; exits `4` when the release has none of that name. `publish-release.sh --replace-files` uses it on a draft | nothing |
| `tag-sha <repo> <tag>` | Finds the commit a tag points to (an annotated tag is followed to its commit); exits `4` when there is no such tag | the commit |
| `delete-branch <repo> <branch>` | Deletes a branch; a branch that is already gone is not an error | nothing |

Run it with a command and its arguments: `bash scripts/forge.sh get-statuses shop-api <sha>`. Without a
command it prints the list of commands and their arguments and exits `2`; an unknown command is refused
(`forge: unknown command '<name>'`); with the wrong arguments it prints that command's usage. The same table
is in the header of the script.

It reads its settings from the environment, and asks the catalog (`python3 scripts/catalog.py forge`) only for
what the environment leaves out, so a Jenkins agent that sets them all needs no `catalog.yaml`:

| Variable | Meaning |
|---|---|
| `FORGE_TYPE` | `forgejo` or `github` (else the catalog's `forge.type`) |
| `FORGE_API_URL` | The REST API's base address (else, for GitHub, `GITHUB_API_URL`, else the catalog's `forge` API) |
| `FORGE_OWNER` | The owner of the repositories (else, for GitHub, `GITHUB_OWNER`, else the catalog's `owner`) |
| `FORGE_TOKEN` | The access token, required (else, for GitHub, `GH_TOKEN`) |
| `GITHUB_UPLOADS_URL` | Where GitHub takes release files (default `https://uploads.github.com`) |
| `YGG_CURL_CONNECT_TIMEOUT`, `YGG_CURL_MAX_TIME`, `YGG_CURL_TRANSFER_MAX_TIME` | How long `curl` waits, in seconds, so a server that hangs cannot hold an agent and the release lock: `10` to connect; `60` in all for an API call; `3600` for an upload (and for `github.sh download-artifacts`), which also stops when it stalls under 1 KiB/s for a minute. `scripts/github.sh` reads the same three |

- The **token is passed to `curl` on its standard input**, never as an argument, so no other user of the
  machine sees it in the process list, and it is never printed.
- **Exit codes:** `0` done; `1` an error, explained on standard error as `forge: <what> failed: <the server's
  message>`; `2` no command at all; `3` (`delete-branch` only) that the server **keeps** the branch because
  it is protected: the owner's decision, not a reason to stop a release; `4` (`find-release`, `tag-sha` and
  `delete-release-file` only) that there is no such release, tag or file.
- **On Forgejo every `release/x.y.z` branch is kept after a release.** `release/*` is protected, and Forgejo
  refuses to delete a protected branch to everyone, the owner included: `delete-branch` exits `3`
  (`forge: kept: release/x.y.z is protected`). Delete old release branches by hand, as the owner: remove the
  `release/*` protection (the repository's **Settings → Branches**), delete the branches, then run
  `python3 scripts/rules.py <repository>` to put the protection back.
- `merge-pr` on Forgejo waits when Forgejo answers "try again later": right after new commit statuses are
  posted Forgejo checks the pull request again in the background and refuses to merge meanwhile, so the
  command retries for about a minute (every 3 seconds, up to 20 times) before it fails. Any other refusal
  fails at once.

## 13. Check everything

| Check | Where | Expected |
|---|---|---|
| Platform containers | `/opt/yggdrasil/scripts/platform.sh ps` on each host | Every service `Up`; `traefik`, `status` and `console` also `(healthy)`. `forgejo` and `jenkins` only on the dev machine (the controller host), `agent` where the profile is on. Run as `ygg` |
| Certificates | `https://yggdrasil.<DOMAIN>` for each host | No browser warning |
| Webhooks | GitHub: the App → Advanced → Recent Deliveries. Forgejo: a repository's **Settings → Webhooks**, its last delivery | Green checks |
| Agents | Jenkins → Manage Jenkins → Nodes | Every agent connected: each host's deploy agent, and `ci` and `release` on the dev machine |
| Jobs | Jenkins → each application's job → *Scan Repository Log* | Ends with `Finished: SUCCESS`, listing `develop`, `main` and the open pull requests. A repository without a `yggdrasil.yml` builds green and says it has nothing to check |
| A pull request's checks | Open a small pull request into `develop` of a repository that has a `yggdrasil.yml` | `branch-policy` and each check appear as statuses on it, first pending, then success, linking to the build |
| Agent can read the env files | `docker exec yggdrasil-agent-1 ls /etc/yggdrasil/<environment>` on each host, for each of its environments | Lists the application env files, no `Permission denied` |
| Console | `https://yggdrasil.<DOMAIN>` | The host, with a chip per environment it runs. The platform system is `up`. Applications are `not deployed` until the first release |
| Status API | `curl -H "Authorization: Bearer <YGGDRASIL_STATUS_TOKEN>" https://yggdrasil.<DOMAIN>/api/status` | JSON with every environment of the host, and every system that has an application in one of them |
| Grafana | `https://grafana.<DOMAIN>` (`grafana-dev.<DOMAIN>` on the dev machine), `admin` / `GRAFANA_ADMIN_PASSWORD` | Prometheus and Loki data sources work |

**Windows and Android consoles**: download them from the releases of
[artur-rios/yggdrasil](https://git.techcraftsmen.net/artur-rios/yggdrasil/releases) (copied to the
[GitHub mirror](https://github.com/artur-rios/yggdrasil/releases)). Your fork has none until you release it: Jenkins
builds them when you push a `vX.Y.Z` tag, the installer through GitHub Actions (`release.windows`), as
[CONTRIBUTING.md](../CONTRIBUTING.md#releasing) describes. Then add each host in
the console's **Hosts** screen with its URL (`https://yggdrasil.<DOMAIN>`) and its
`YGGDRASIL_STATUS_TOKEN`: one entry shows every environment of the host.

## 14. The first release

In an application repository prepared in step 5:

```bash
git switch develop && git pull
git switch -c release/0.1.0 && git push -u origin release/0.1.0
```

1. Environments whose `branches` match `release/*` deploy it (homologation, in the default
   catalog). Follow it in Jenkins, in the application's job, on the `release/0.1.0` branch. An
   on-demand environment that is stopped is stopped again once the new version is healthy: run
   `ygg env start homologation` on its host to try it
   ([On-demand environments](#on-demand-environments)).
2. Open the pull request `release/0.1.0 → main`. Once its checks pass, Jenkins deploys it to each
   release environment in order, asking for approval where the catalog says so (**Deploy** in the
   build's page).
3. After the last one, Jenkins merges the pull request and tags `v0.1.0`. A repository with a
   `yggdrasil.yml` that declares `release` then has its files built, signed and validated on the release
   agent, and the release is published with them; without `release` the release has the notes only. On
   GitHub, a repository without a `yggdrasil.yml` instead gets a direct release, or with
   `releaseArtifacts: true` an immutable tag and private draft that its release-artifact workflow
   publishes. Jenkins then deletes the branch (Forgejo keeps `release/*`: delete old ones by hand). The
   console shows the version, commit and deploy time. Every step: [Releases](#releases).

If a deploy fails, it rolls back to the previous image and the pull request stays open. A failure after
the merge says that the pull request is merged and tagged, and how to finish: the `release-resume` job
([Finishing a release that stopped](#finishing-a-release-that-stopped)).
The build's console output says why: usually a missing env file variable, a health check that never
passes, or a failed check or release build. Fix it on `develop` (or on the host, for an env file) and cut
a new release.

Day to day from here: [README.md#day-to-day](../README.md#day-to-day).

## Where Jenkins and the hosts read the catalog

| Reader | Reads |
|---|---|
| Jenkins controller: jobs, agents, what a build deploys where | The controller host's `/opt/yggdrasil/catalog.yaml` |
| Each deploy: `scripts/deploy.sh` and `stacks/` | A fresh clone of your fork's `main`, made by the agent for the build |
| Every build: the pipeline library (`jenkins/library/`) | Your fork at the tag `YGGDRASIL_LIBRARY_VERSION` ([How a build runs](#how-a-build-runs)); the `scripts/` it calls at the same tag, `catalog.yaml` and `stacks/` from `main` |
| A repository's checks and releases | Its `yggdrasil.yml` at the commit being built (data only), checked against the controller's `catalog.yaml` |
| `platform.sh`, the status API, Prometheus, hand deploys | That host's `/opt/yggdrasil` |

So a change to the catalog or the stack files goes to `main` first, then to every host, as `ygg`:

```bash
cd /opt/yggdrasil && git pull
scripts/platform.sh up
docker restart yggdrasil-status-1                        # every host: re-reads the catalog
docker restart yggdrasil-jenkins-1                       # the controller host only
```

Variables are not part of this: `deploy.sh` and `platform.sh` read them from the variables store
or the env files each time they run, so a changed variable needs no restart, only the next deploy of
the application (or `platform.sh up` for the platform's own).

`platform.sh up` alone isn't enough: the catalog is mounted into the running containers as a single
file, and they keep seeing the old one until they restart. Jenkins creates new jobs and agents only
when it starts; it never deletes agents (remove them in **Manage Jenkins → Nodes**).

## The pipeline

What Jenkins does with a repository, from a push to a release. The scripts it calls are in `scripts/`, and
the library is `jenkins/library/vars/yggdrasilPipeline.groovy`, whose header tells the same story from the
code's side. A repository takes part by having a [`yggdrasil.yml`](catalog.md#repository-pipeline-file).

### How a build runs

**Jenkins never reads a repository's `Jenkinsfile`.** Every job (one per application and per
[`repositories`](catalog.md#repositories) entry, named after its catalog id) runs the one
[`jenkins/Jenkinsfile.default`](../jenkins/Jenkinsfile.default), which Configuration as Code keeps inside Jenkins
(Config File Provider, `yggdrasil-default`) and runs in the Groovy sandbox. It only loads the yggdrasil library
and calls it. Why: a branch pushed by anyone who is not the owner (the `claude` account, a Renovate pull
request) would otherwise run its own pipeline code, with every credential in Jenkins within reach. What a
repository controls is **data**: its `yggdrasil.yml` names images, commands, globs and the *names* of
credentials. The library decides what to do with them, runs the commands only inside containers on the `ci`
agent (checks) or the `release` agent (releases), and binds a credential only if the catalog allows that
repository that id. A `Jenkinsfile` left in a repository is ignored.

The library is **pinned**: Jenkins loads `jenkins/library/` from your fork at the tag
`YGGDRASIL_LIBRARY_VERSION`, so what runs in your pipelines is a release you chose, not whatever `main` is
today. Each build also clones your fork for what the library calls, split in two:

- **the code** (`scripts/`, and everything else of yggdrasil) at the **same tag**, so the library and the
  scripts it runs are always one release, whatever has been merged into `main` since;
- **your data**, `catalog.yaml` and `stacks/`, from **`main`**, where you keep them up to date: a new
  application's stack file or a catalog change is used by the next build without a new release. (The
  controller's own copy of the catalog, `/etc/jenkins/catalog.yaml`, is what decides the jobs, agents and
  allow-lists; the clone's copy is what `deploy.sh` reads on the deploy agents.)

A change to `scripts/` therefore reaches the pipeline only with a new tag. To upgrade the pipeline: release yggdrasil in your fork ([CONTRIBUTING.md](../CONTRIBUTING.md#releasing)),
set `YGGDRASIL_LIBRARY_VERSION` to the new tag in the controller's `platform.env`, and run
`scripts/platform.sh up` (Compose recreates the controller).

A job sees `develop`, `main` (and `master` when its repository is under the `basic` rules: a fork's default
branch, treated like `main`), pull requests from branches of the repository itself (a fork's never run),
`release/*`, the `branches` of its branch environments and, for a `release: owner-tag` entry, tags that match its
`tags`. A build lasts at most 4 hours and the last 50 are kept. A newer push to a pull request that is not into
`main` replaces the build still running for the older commit; release builds of one repository wait for each
other instead (one lock per repository, so two releases never overlap).

### What happens when

| Event | A repository **without** `yggdrasil.yml` | A repository **with** one |
|---|---|---|
| Pull request into `develop` (or, under the `basic` rules, into `main` or `master`) | Nothing (on GitHub the Branch Policy workflow and Actions check it) | `branch-policy`, then the job's checks (its catalog entry's `checks`), as statuses on the head commit; the build fails once all have reported if any failed. On Forgejo with a `mirror`, the `actionsChecks` are copied from GitHub Actions too ([below](#github-actions-and-the-skip)) |
| Pull request into `main` from anything but `release/x.y.z` | Nothing | `branch-policy` fails it (a `basic` repository passes it) |
| Release pull request `release/x.y.z` → `main` | GitHub: every GitHub Actions check waited for, each release environment deployed, then merged, tagged and released as before. Forgejo: the same steps without release files | The pipeline of [Releases](#releases) below: checks, GitHub Actions checks (or the skip), deploys, merge, tag, files, publication |
| Push to `develop`, `main` or `release/*` (and `master` under the `basic` rules) | Deploys to the environments whose `trigger: branch` globs match | The job's checks first, and its `deploy.inputs` when it deploys (reused or skipped where the rules allow); the deploys only if every check passed, and of the commit the checks saw |
| Push to another branch that a `branches` glob matches | Deploys | Deploys, with no checks |
| Tag matching an owner-tagged entry's `tags`, at most a day old | The tag is read on the release agent and must be on `main`; nothing else happens | What the file declares: release files and/or NuGet packages ([Owner tags and NuGet](#owner-tags-and-nuget)) |
| **Build with Parameters → `DEPLOY_TO`** | Deploys the branch to a `manual` or `branch` environment, no checks | The same |
| Any other pull request | Nothing | Nothing |

An application with no `trigger: release` environment is never merged or released by Jenkins.

### Branch policy and checks

The branch policy is `scripts/branch-policy.sh`, run on the `ci` agent, then the repository's own
`branchPolicy` (an `image` and a `run`) if its file has one. It is reported as the status `branch-policy` and
is never reused. A repository under the `basic` rules ([`rules`](catalog.md#repositories): a site deployed on pushes
to `main`, a fork, a repository whose entry says `rules: basic`) has no branching model to follow, so it passes
for any pull request, with `basic: nothing to check`, and neither the script nor the file's own `branchPolicy`
runs. Its `master`, a fork's default branch, is treated like `main`: a pull request into it gets the branch
policy and the checks, and a push to it the checks. Under the release model:

- into `develop`: only `feature/<name>`, `fix/<name>` or `renovate/...` (it replaces `dependabot/...`), cut
  from `develop` (a branch that contains a release merge commit from `main` was cut from `main`);
- into `main`: only `release/x.y.z`, a snapshot of `develop` (every commit already on it), whose version has
  no `vx.y.z` tag.

A job runs its own catalog entry's `checks`, and the checks `deploy.inputs` names when it deploys; the
file's other checks are another entry's of the same repository, and that job runs and reports them
([Repository pipeline file](catalog.md#repository-pipeline-file)). Each check runs through
`scripts/run-check.sh`, in its `image`, as root of the `ci` user's engine (the service user on the host), on
the checkout, with its `run` line given to `sh -c`; `docker: true` also gives it that engine's socket
(Testcontainers). Up to four run at once. Its `credentials` are written to a private file and handed to the
container as environment variables: only ids the catalog allows the repository, never one of the pipeline's
own (`forge-*`, `github-*`) or the NuGet key, and **never on a `renovate/*` build**. Its test results
(`junit`) and `archive` are collected whatever the outcome, unless a symbolic link in the checkout leads out
of it (the build is then unstable and collects nothing).

Every check is posted from the controller as a commit status on the build's commit: `pending`, then `success`
or `failure`, with the build's page as its address and the check's name as its context. A check skipped by
its `paths` is posted `success` with the reason. A check that already has a `success` **created by
`forge.jenkinsUser`** for this commit is **reused** and not run again; a failure, a pending, or a success
created by anyone else is never trusted. On GitHub those statuses are created by the App's bot login
(`<app-slug>[bot]`), so set `forge.jenkinsUser` to it to get reuse, or every check runs again. A push's changed
files are counted from the last commit of that branch whose checks passed.

**A pull request whose base branch is changed is not checked again by itself.** Jenkins builds a pull
request's head (not its merge with the base) and ignores a change that touches only its target
(`ignoreTargetOnlyChanges` in the Job DSL), so retargeting one, say from `develop` to `main`, starts no build.
Its statuses, `branch-policy` among them, are still the ones checked against the old base, and since a status
belongs to the commit, they can satisfy the new base's required checks. Recheck it by hand: in Jenkins, open
the repository's job, **Scan Repository Now** (Jenkins learns the new base), then the pull request's `PR-<n>`
job, **Build with Parameters** with `DEPLOY_TO` left empty; or push a new commit to its branch. Only
`forge.jenkinsUser` merges into an application's `main`, and only a release pull request it built itself, so
this matters for `develop` and for the `main` of an `owner-tag` repository, which you merge.

### GitHub Actions and the skip

Windows and macOS builds cannot run in Jenkins; they stay GitHub Actions jobs, the catalog's `actionsChecks`.
On GitHub they report by themselves. On Forgejo they run on the repository's GitHub **mirror**, and Jenkins
copies their results: on a pull request into `develop` it looks at GitHub once a minute, up to `checksTimeout`,
from the publish agent and without holding an executor while it waits, and posts each job as a status on the
Forgejo commit with the GitHub run as its address. If they have not finished in time the status stays pending
(`GitHub Actions didn't answer`) and the build is unstable; a new build looks again.

**Their triggers.** The pull requests live on Forgejo; the mirror has none, it only receives every branch the push
mirror pushes. Jenkins looks up the job's run for the pull request's head commit by its SHA. So on Forgejo with a
`mirror`, an `actionsChecks` job runs on `push` to every branch, and on nothing narrower:

```yaml
on:
  push:
    branches: ['**']
  workflow_dispatch:
```

No `pull_request` (it never fires on the mirror) and no `paths` (a commit the filter skips has no run, so its
status stays pending). A release build's Windows workflow (`release.windows`) is different: Jenkins dispatches it
on the tag, so it has only `workflow_dispatch` with a `tag` input.

A release pull request waits for the `actionsChecks` the same way, for up to the first release environment's
`checksTimeout` (one hour by default). One that failed stops the release (`Not releasing`). One that did not
finish, because it is still running, GitHub did not answer, or the mirror has not pushed the commit,
makes Jenkins ask, outside any agent: **"GitHub Actions checks didn't finish. Deploy without them?"**, with the
button **Deploy without GitHub Actions checks**. Abort fails the release before anything is deployed. A release
that declares both `release.windows` and `release.finalize` is **never asked**: the skip would publish it
without its Windows files, which `release.finalize` must cover, so the build fails there instead (`Not
releasing`), before anything is deployed; build the pull request again once GitHub Actions answers. After the
skip:

- it is recorded in the build log, in the build's description (`v1.4.0 (deployed without GitHub Actions
  checks)`) and in the descriptions of the `deploy/<environment>` statuses;
- no Windows files are built, since GitHub may be down: a release that declares `release.windows` is
  published without them, its notes list the missing files, and the build is **unstable**. With a `mirror`
  that copies releases, the release is published on **Forgejo only** and the build's description says
  `GitHub copy pending`;
- finish with the `release-resume` job (`REPOSITORY`, `TAG`): it makes the GitHub copy, with the Windows files
  ([below](#finishing-a-release-that-stopped)). The `actions-release` job attaches Windows files to a release
  that exists on both servers; while the GitHub copy is pending it attaches them on Forgejo only and says to
  run `release-resume`. Either way the notes keep their *Missing files* section: delete it by hand afterwards
  ([below](#finishing-a-release-that-stopped)).

### Releases

A release pull request (Forgejo, or a GitHub repository with a `yggdrasil.yml`) goes through these steps, under
the repository's release lock:

1. Its **branch policy and checks**, as any pull request, except that the checks are every one `main`
   requires of the repository: each catalog entry's `checks` when it backs several (a docs site's too), then
   `deploy.inputs`. Production is deployed before the merge, so a check that would block the merge must pass
   first. A failure: `Not releasing`. The repository's other jobs build the same pull request with their own
   checks only, and a failure there reads `Failed: <checks>.`
2. The **`actionsChecks`**, and the skip above.
3. Every `deploy/<environment>` is set `pending`, then each release environment is **deployed in catalog order**
   (a click on **Deploy** first where `approval: true`, waiting outside any agent), `deploy/<environment>` set
   `success` after each. A failed deploy rolls back, sets `failure` and stops. The commit is the pull request's
   head; if it moves, the build fails.
4. On the publish agent, the **merge** (titled `release: vx.y.z (#N)`, refused if the head moved) and the
   **tag** `vx.y.z` on the merge commit.
5. The **release files**, when the file declares `release`: `release.build` runs on the release agent from
   the tag, with only the credentials `release.build.credentials` names (each allowed by the catalog entry's
   `releaseCredentials`), and each of its `files` globs is
   stashed (a glob that matches nothing fails the release); `release.windows` has the workflow dispatched on
   GitHub once the mirror has pushed the tag, and its files downloaded; everything is gathered on the release
   agent and `release.validate` runs over a copy. Validation is **pass or fail only**: what is uploaded is
   the set taken before it ran, so it cannot change the files, and a failure stops the release before any
   draft exists. A Windows run that fails or times out leaves the release to go on without those files
   (unstable, listed in the notes).
   Then **`release.finalize`**, when declared, on the release agent: what has to cover the whole set (a
   manifest of every file with its checksum, the manifest's signature). It sees the set as the validation
   does (`RELEASE_FILES`, `build/` and `windows/` under it, a fresh copy of the gathered set), and runs in a
   workspace of its own: a fresh checkout of the tag and of yggdrasil's tools, since the validation ran as root
   with its whole workspace (yggdrasil's scripts included) and nothing it left there may reach a step that
   holds credentials. It gets `RELEASE_TAG`, `RELEASE_VERSION` and only the credentials
   `release.finalize.credentials` names (each allowed by `releaseCredentials`, as for the build), and each
   of its `files` globs, matched in the checkout, is added to the upload set under `finalize/` (a glob that
   matches nothing fails the release). It may **only add**: the set is checksummed before and after it
   runs, and a file changed, removed or added there (rather than declared in `files`) fails the release,
   naming it. A failure of any kind stops the release before any draft exists, as a failed validation
   does, and `release-resume` runs it again. It never covers a partial set: with `release.windows` too, a
   Windows run that fails or times out **stops the release** instead of letting it go on without those files
   (*the pull request is merged and tagged…*, as above), and `release-resume` runs the Windows workflow, the
   validation and `release.finalize` again on the whole set. Such a release is never offered the skip
   ([below](#github-actions-and-the-skip)).
   Every file of the upload set (the release files and `release.finalize`'s) must have its **own name**: a
   release shows one file per name. Two files named alike, in any directories, fail the release here, naming
   them, before anything that cannot be undone (a library's NuGet push, a draft).
6. On the publish agent, the **publication**: the notes (the version's `CHANGELOG.md` section, else the titles of
   the pull requests merged since the previous tag), a draft release on Forgejo and, with a `mirror` that
   releases (the default), on GitHub, every file uploaded and checked by name and size on each; then Forgejo's
   is published, and GitHub's **last**, so a release that is public on GitHub is complete everywhere.
   A release whose version has a SemVer **pre-release** part (`1.2.0-alpha.1`, `v1.2.0-rc.2`; for a
   `<package>@<version>` tag, the version after the last `@`) is made a **prerelease** on both servers, so
   GitHub never makes it the latest release; any other version is a normal release, build metadata alone
   included (`1.2.0+build.5`). The notes are the same either way. The rule is strict SemVer
   (`X.Y.Z-pre[+build]`, no leading zeros): `v1.2-rc.1`, not a SemVer version, is a normal release. Only an
   owner's tag can be a pre-release: an application's version comes from its `release/X.Y.Z` branch, which
   has no pre-release part.
7. The **release branch is deleted**. Forgejo keeps it (`release/*` is protected, `delete-branch` exits `3`): a
   note in the log, not a failure; delete old release branches by hand.

The order is the point: nothing is published before the files are validated and finalized, and the release is
visible on GitHub only when it is whole.

### Finishing a release that stopped

After the merge a release cannot be run again (the merge would be refused), so a failure from there on says
how to finish it: *the pull request is merged and tagged `vx.y.z`; finish the release with the release-resume
job (`REPOSITORY=<id>`, `TAG=vx.y.z`)*. In Jenkins, open the **`release-resume`** job and **Build with
Parameters**: `REPOSITORY` is the catalog id (an application with a release environment, or an `owner-tag`
entry) and `TAG` the tag, which must exist and be on `main`. It does what a release does after its merge, from
the tag: builds, validates and finalizes the files, pushes a library's packages, and publishes the release and its
GitHub copy. It is safe to run twice: the tag, the drafts and the files already there are reused, a release already
published is left alone, and NuGet versions the feed has are skipped. Use it also to make the GitHub copy of a
release published after the skip, and for a tag that was never built.

Two cautions. It **rebuilds** the files from the tag instead of copying the published ones, so a build that is
not reproducible can give a file with the same size and other bytes than the one already on Forgejo, and the
GitHub copy would then differ in content; a different size stops the run (on a published release, with a message
that these files belong in a new release, a new version: nothing of a published release is deleted). A release with `release.finalize` is
the exception for its **drafts**: their manifest and signature are made again in this run, so a draft's files are
deleted and uploaded again (`publish-release.sh --replace-files`) rather than reused by name and size, and a
draft's files whose names are not in the run's set are deleted, so a draft never keeps a file its new manifest
does not describe. A published release is never changed. One consequence: if Forgejo's release was published and
publishing GitHub's copy then failed, `release-resume` uploads GitHub's draft again from its own run, so the two
copies can hold **different bytes** (a build that is not reproducible), each one matching the manifest it carries;
compare the two manifests if that matters.
And if the merge went through but the
tag did not, the message says *merged as `<sha>`, but not tagged* and names a third parameter: run
`release-resume` with `COMMIT=<sha>` too. Only the Jenkins user may create an application's `v*` tags (on
Forgejo not even you), so the job makes it: it checks that the commit is one of `main`'s own merges (on its
first-parent line, not a commit merged into it from a branch), is a merge commit and is titled
`release: vx.y.z (#N)` for that very `TAG` (only Jenkins merges into an application's `main`, and only a release
pull request's merge has that title there, so no other commit can be tagged this way — not even a hand-made
commit with that title brought in through `develop`), then tags it on the
publish agent and goes on as above. A tag of that name already on the commit is kept; one on another commit
stops the job. `COMMIT` is refused for an `owner-tag` repository, whose tags are yours to make.

The **`actions-release`** job (`REPOSITORY`, `TAG`) runs the Windows workflow again for a tag and attaches its
files to a release that was published without them. It refuses a release that declares both `release.windows` and
`release.finalize`, whose Windows files are covered by its manifest and so are never attached afterwards, and
points to `release-resume`, which makes the release whole.

The notes are not rewritten afterwards: a release published without its Windows files keeps the
*Missing files* section of its notes after `actions-release` or `release-resume` has attached them (both
reuse the release, notes and all, on Forgejo and on GitHub). Once the files are there, edit the release
on each server and delete that section by hand.

### Owner tags and NuGet

A `repositories` entry with `release: owner-tag` (a library, yggdrasil itself) is released by its owner tagging a
version on `main` that matches the entry's `tags` (`v*` by default, `[0-9]*` for versions such as `1.4.0`). Create
the tag **annotated**:

```bash
git switch main && git pull
git tag -a 1.4.0 -m 1.4.0
git push origin 1.4.0
```

Jenkins builds only tags at most a day old (a new job's first scan would otherwise rebuild and release every old
tag), and it dates a **lightweight** tag by its *commit*, not by when it was pushed. A lightweight tag on a commit
older than a day is therefore never built, silently. If a tag was not built, run `release-resume`.

The tag build, under the repository's release lock, reads the tag on the release agent and checks that its commit
**is on `main`** (`the tag is not on main` stops it), then does what `yggdrasil.yml` declares at the tag: nothing
without the file; the release files and the release of [Releases](#releases), steps 5 and 6, with `release`; the
packages below with `publish.nuget`. A library with `publish.nuget` and no `release` publishes no release on the
git server: its packages are its release.

**NuGet.** `publish.nuget` (`image`, `run`, `packages`, default `**/*.nupkg`) takes two steps on the release agent.
First the packing: `run` in `image`, with **no credential at all**, and the packages stashed. Then the push, in a
fresh workspace that holds this repository's `scripts/` and the packages only: `scripts/nuget-push.sh` runs
`dotnet nuget push --skip-duplicate` in a pinned .NET SDK image, with the Jenkins credential `nuget-api-key`
bound for this step alone, where none of the repository's code runs. A version the feed already has is skipped,
so a rerun goes on. When the library also declares `release`, the packages are pushed after the release files are
built, validated and finalized and before the release is published: a push cannot be taken back, so a release
whose files fail pushes nothing.

- The credential `nuget-api-key` exists when `NUGET_API_KEY_FILE` is set ([9.2](#92-the-env-files-first-pass));
  without it a library's tag build packs, then fails at the push, naming the credential. Its id is fixed, and no
  check or release build can bind it.
- The push goes to nuget.org. `YGG_NUGET_SOURCE` (a feed's service index, https only) and `YGG_NUGET_CA_FILE` are
  read from the **release agent container's own environment** and exist for the live tests, which push to a local
  feed. `platform/build-agent.yml` does not pass them on, so they are an operator's edit of that file. Don't leave
  them set in production unless you mean it: the feed they name receives the API key.

### Accepted risks

- **Version tags on Forgejo can be moved or deleted by whoever creates them** (`forge.jenkinsUser` for an
  application, you for an `owner-tag` repository): one Forgejo list covers creating, moving and deleting. The
  daily `tag-check` job finds a moved or deleted release tag, and the GitHub copies, where nobody can move or
  delete one, are what to repair it from ([Version tags: tag-check and repairs](#version-tags-tag-check-and-repairs)).
- **Checks run branch code, side by side.** Any branch's `yggdrasil.yml` can set `docker: true`, and through the
  shared `ci` engine a check can then read the env files and workspaces of checks running at the same moment. So
  a credential you allow a repository's checks (`credentials` in the catalog) is reachable by anyone who can push
  a branch to a repository while one of its checks runs. Allow none you would not give them. Jenkins' write tokens
  are never reachable this way: the `ci` agent never holds them, and a `renovate/*` build gets no credential.
- **No per-job authorization.** Jenkins lets every logged-in user do anything (`loggedInUsersCanDoAnything`), and
  the owner is its only user. Anyone you add can approve a deploy, run `release-resume` or read a credential.
- **Webhooks are not verified** by Jenkins ([9.4](#94-jenkins-and-the-webhook)): a forged one only starts a scan.
- **On GitHub without `github-read`**, the `ci` agent checks out with the main App's token, which can write
  (every build log says so). Set `GITHUB_READ_APP_ID` and `GITHUB_READ_APP_KEY_FILE` ([9.2](#92-the-env-files-first-pass)).
- **The NuGet key is in `dotnet`'s arguments** inside the push container (`--api-key`), so a process list in the
  release user's engine shows it; that engine runs only builds the owner started.
- **A harmless stack trace**: the Gitea plugin tries to post a status with the read token when a build checks out,
  Forgejo answers 403, and the build log shows the trace. It cannot be turned off.
- **What is not proven.** `scripts/test_jenkins_live.py` runs the pipeline against a real Jenkins and Forgejo
  ([CONTRIBUTING.md](../CONTRIBUTING.md#live-suites)), with GitHub replaced by a double and on a rootful Docker
  engine. The GitHub paths and rootless Docker run for the first time on your hosts: read the first builds.

## On-demand environments

An environment with `onDemand: true` (development and homologation in the default catalog) runs
only while someone uses it. On its host:

```bash
ygg env start development     # starts every application of development
ygg env stop development      # stops them again
ygg env status                # which environments of this host are on
```

| | While it is stopped | While it is on |
|---|---|---|
| A Jenkins deploy on a push (`trigger: branch`) | Builds the new version, starts it, waits for it to be healthy (rolling back a broken one as usual), then stops it again: the next `env start` runs it. The build log says `deploy: development is on demand and <application> was not running: stopped it again` | Deploys and leaves it running |
| **Build with Parameters → `DEPLOY_TO`** (on `develop`, a `release/*` branch or `main`), or `DEPLOY_START=1 scripts/deploy.sh ...` | Deploys and leaves it running | The same |
| The console and the status API | `stopped`: neutral, not a problem, not probed | Its applications' health, as anywhere |

`env stop` refuses an environment that isn't on demand (production) unless you add `--force`.
Details: [cli.md](cli.md#start-and-stop-environments).

## Adding things

### An application

`ygg add` does 1, 2 and 4 on the host you run it on, and prints the rest
([cli.md](cli.md#set-up-an-application)).

1. **Catalog:** add it under its system (or a new system) in `catalog.yaml`: `id`, `kind`, `health`, and if they apply `host`, `metrics` and `checks`. Add `environments` only if it doesn't deploy everywhere.
2. **Stack files** in `stacks/`, as in [step 4](#4-write-the-stack-files).
3. **The application's repository**, as in [step 5](#5-prepare-each-application-repository). Add it to the GitHub App's repositories ([step 7.5](#75-install-the-app-on-the-repositories)).
4. **Env files** on each host it deploys to, as in [step 11](#11-application-env-files).
5. **Apply:**
   - push the catalog and stack files to `main` of your fork
   - `python3 scripts/rules.py <repository>` (the new repository; on an existing GitHub installation run it
     for a repository only once it has adopted `yggdrasil.yml`, see [An existing GitHub installation](#an-existing-github-installation))
   - on every host, pull and restart as in [Where Jenkins and the hosts read the catalog](#where-jenkins-and-the-hosts-read-the-catalog): Jenkins creates the new job, the status APIs and Prometheus pick it up

### An environment

**On a host that already runs others** (a `staging` next to development, homologation and
production on the VPS, say): no new platform, agent or DNS record.

1. **Catalog:** add it to `environments`, in promotion order, with the host's `agent` (`vps`), a
   `hostSuffix` of its own (`-stg`), `onDemand: true` if it should run only while used, and its
   other options. Push to `main`.
2. **Host:** pull, add it to `ENVIRONMENTS` in `platform.env`
   (`development,homologation,staging,production`), then `scripts/platform.sh up` and restart the
   status API (and, on the controller host, Jenkins), as in
   [Where Jenkins and the hosts read the catalog](#where-jenkins-and-the-hosts-read-the-catalog).
   The DNS record and the certificate already cover `<host>-stg.<DOMAIN>`.
3. **Env files:** for each application, as in [step 11](#11-application-env-files), with the
   suffixed host names (`PUBLIC_HOST=shop-api-stg.example.com`).
4. `python3 scripts/rules.py`, if it's a release environment: `main` now also requires its
   `deploy/<id>`. On an existing GitHub installation, add `deploy/<id>` by hand to the `main` ruleset of each
   repository that has not adopted `yggdrasil.yml` yet (**Settings → Rules → Rulesets → main**): running the
   script for it would unbind its checks, see [An existing GitHub installation](#an-existing-github-installation).

**On a host of its own:**

1. **Catalog:** add it to `environments`, in promotion order, with its options, and push to `main`.
2. **DNS:** the host's `*.<DOMAIN>` record, as in [step 6](#6-domain-and-dns).
3. **Host:** prepare it as in [step 8](#8-prepare-each-machine).
4. **Apply:**
   - On the controller host, pull and restart Jenkins as in [Where Jenkins and the hosts read the catalog](#where-jenkins-and-the-hosts-read-the-catalog). Jenkins creates the new agent.
   - Bring the new host up as in [step 10](#10-the-apps-machine), with the new agent's secret.
   - `python3 scripts/rules.py`, if it's a release environment: `main` now also requires its `deploy/<id>`.
     On an existing GitHub installation, add it by hand to the `main` rulesets of the repositories that
     have not adopted `yggdrasil.yml`, as in the case above.
5. **Env files:** for each application on the new host, as in [step 11](#11-application-env-files).

### A system

A system is only a grouping: add it with its applications to `catalog.yaml`, then add the applications as above.

## Observability

- **Grafana** at `https://grafana.<DOMAIN>`, with Prometheus and Loki provisioned. Good starting dashboards to import: *ASP.NET Core* (19924), *Traefik* (17346), *Jenkins* (9964).
- **Metrics**: every catalog application with `metrics` is scraped, once per environment of the host, labelled `system`, `app`, `kind` and `environment`, through the status API's service discovery. Platform components are scraped once, without `environment`. Serve metrics on a port that is neither published nor routed.
- **Logs**: Alloy ships every container's stdout to Loki, labelled `environment` (from the container's `yggdrasil.environment` label; platform containers have none), `stack` (the Compose project, `<application>-<environment>`), `service`, `container` and `level` (read from JSON logs' `Level`/`level`). For example: `{environment="production", stack="shop-api-production"} | json | Level="Error"`.

## Troubleshooting

A service's log, as `ygg`: `docker logs yggdrasil-<service>-1 2>&1 | tail -50`, e.g. `yggdrasil-jenkins-1`,
`yggdrasil-agent-1`. (`platform.sh logs <service>` follows it live; Ctrl+C to stop.)

| Symptom | Likely cause |
|---|---|
| `required variable ... is missing a value` | A variable of `platform.env` is empty: see [9.2](#92-the-env-files-first-pass) |
| `platform: '...' (ENVIRONMENTS in ...) is not an environment in catalog.yaml` | A typo in `ENVIRONMENTS` (comma-separated, no spaces), or the host's checkout is behind: `git pull` in `/opt/yggdrasil` |
| `platform: JENKINS_AGENT_NAME must be set ...` (or `JENKINS_AGENT_SECRET`) | `COMPOSE_PROFILES` includes `agent` before the agent exists: see [9.5](#95-the-env-files-second-pass-this-hosts-agent) and [10](#10-the-apps-machine) |
| `Cannot connect to the Docker daemon at unix:///var/run/docker.sock` | `docker` is using the machine's own engine, which is switched off and masked. There is no `docker` group and no rootful engine: Docker belongs to `ygg`. Log in as `ygg` (`ssh ygg@<host>`), whose login shell points `docker` at its own engine |
| `permission denied` on `/run/user/<uid>/docker.sock` | You are another user than the socket's owner. A rootless socket is private to its user: the admin cannot use `ygg`'s engine, and `ci` cannot use `ygg`'s (`ygg check` makes sure of that). Run the command as the user the engine belongs to; `ygg agent …` does that for `ci` and `release` |
| `no rootless Docker engine for <user>: run 'ygg install'` (from `platform.sh`, `agent.sh` or `backup.sh`) | The user has no running engine: `ygg install` was not run for this role, or its `docker.service` stopped (`systemctl --user status docker`, as that user). A `docker` run by `sudo -u` also needs `XDG_RUNTIME_DIR` set |
| `the Docker engine at ... runs as root; the platform runs on rootless Docker only` | `DOCKER_HOST` or the socket points at a rootful engine. Use the user's own ([hosts.md](hosts.md#5-rootless-docker)) |
| Traefik's access log, or heimdall-api's rate limit, sees one address (Cloudflare's, or an internal one) for everyone | Two different causes. **An internal address** for every client: the engine is not on `slirp4netns`. `ygg check` shows the "client addresses" row; `ygg install` writes `override.conf`, and the engine must restart after it (`systemctl --user restart docker`, as that user). **Cloudflare's address**, on the apps machine: `CLOUDFLARE_PROXIED` is `false`, so Traefik trusts no forwarded header. Set it `true`, make sure `/etc/yggdrasil/cloudflare-ips` exists (`ygg install --role apps`), and `platform.sh up`. A client that reaches the machine without Cloudflare is refused by the firewall |
| The engine is not running after a reboot, or stops when you log out | Linger is off for that user: `loginctl enable-linger <user>` (`ygg install` does it; `ygg check` shows the "linger" row). Without it the user's systemd, and the engine under it, stop with its last session |
| `blocked request` in the log of a `yggdrasil-*docker-proxy-1`; with it the status API logs `docker proxy answered 403`, Traefik loses its routes or Alloy stops shipping logs | That client asked Docker for something its proxy's allowlist in `platform/compose.yml` doesn't allow, typically after an upgrade of Traefik or Alloy. The log line names the request: check it against the client before adding it ([status-api.md, Docker access](status-api.md#docker-access)) |
| `/etc/yggdrasil/github-app.pem` (or `forge-write-token`, `forge-read-token`, `forge-webhook-secret`, a `NUGET_API_KEY_FILE`) is a directory | `platform.sh up` ran with the `jenkins` profile before the file existed, and Docker created a directory in its place. `sudo rm -r` it, then do [9.1](#91-the-github-app-key) or [9.7](#97-the-forgejo-accounts-and-tokens) |
| Jenkins starts, but with no jobs or credentials errors | `GITHUB_APP_ID` wrong, the key not converted to PKCS#8, or not readable by uid 1000 ([9.1](#91-the-github-app-key)). The Jenkins log says which |
| A job's scan finds no branches | Jenkins' read credential cannot see the repository: on GitHub the App isn't installed on it, on Forgejo `jenkins-ci` has no access to it ([9.7](#97-the-forgejo-accounts-and-tokens)). A branch only appears if it is `develop`, `main`, `release/*`, a pull request, or matches an environment's `branches` |
| Every build fails loading the library (`yggdrasil` not found at a version) | `YGGDRASIL_LIBRARY_VERSION` is not a tag of your fork that Jenkins' read credential can read (`git tag --list 'v*'`); fix it in `platform.env` and `platform.sh up` ([How a build runs](#how-a-build-runs)) |
| A check starts but its container has an empty workspace, or a build waits for a `ci` executor forever | The `ci` agent is offline, or its `YGG_AGENT_WORKDIR` differs from `CI_AGENT_WORKDIR` in the controller's `platform.env` ([9.8](#98-the-ci-and-release-agents)) |
| `Not released: the pull request is merged and tagged …` | The release failed after its merge. Run the `release-resume` job with the repository and the tag it names ([Finishing a release that stopped](#finishing-a-release-that-stopped)) |
| `Not released: the pull request is merged as <sha>, but not tagged …` | The merge went through and the tag did not. Run `release-resume` with the repository, the tag and `COMMIT=<sha>`, as the message says: it tags the merge commit, then releases ([Finishing a release that stopped](#finishing-a-release-that-stopped)) |
| The build waits at **Deploy without GitHub Actions checks** | The GitHub Actions checks did not finish; click the button to go on without them (Forgejo only publication, `release-resume` later) or **Abort** ([GitHub Actions and the skip](#github-actions-and-the-skip)) |
| An owner's tag built nothing | The tag is lightweight and on a commit older than a day, or does not match the entry's `tags`, or the repository is not `release: owner-tag`. Tag with `git tag -a`, or run `release-resume` ([Owner tags and NuGet](#owner-tags-and-nuget)) |
| The `tag-check` job is red | A release's tag moved or was deleted: its log ends with a `MOVED` or `DELETED` line for each. Repair them from the GitHub copy ([Version tags: tag-check and repairs](#version-tags-tag-check-and-repairs)) |
| A Forgejo push mirror fails to sync after a tag changed | GitHub refused to move or delete a version tag (`mirror tags`): intended. Repair the Forgejo tag ([Version tags: tag-check and repairs](#version-tags-tag-check-and-repairs)) |
| A stack trace with `403` at every checkout in a build log | The Gitea plugin posting a status with the read token. Harmless ([Accepted risks](#accepted-risks)) |
| An agent stays offline | Wrong `JENKINS_AGENT_SECRET` or `JENKINS_AGENT_NAME`, or `JENKINS_URL` not reachable from that host. The agent's log says which |
| A new application or environment doesn't appear in Jenkins or the console | The containers still see the old catalog: restart them as in [Where Jenkins and the hosts read the catalog](#where-jenkins-and-the-hosts-read-the-catalog) |
| `deploy: missing or unreadable env file ...` | The application's env file isn't at `/etc/yggdrasil/<environment>/<application>.env`, or the agent can't read it: `ygg` must own the directories (`750`) and the file (`600`) ([8.3, part 9](#83-install), [11](#11-application-env-files)). A host moved from the earlier guide still has the old owner and group: [hosts.md, Upgrading](hosts.md#9-upgrading-a-host-from-rootful-docker) |
| `platform: ENVIRONMENTS is set but COMPOSE_PROFILES lacks apps` | Add `apps` to `COMPOSE_PROFILES`: the status API and the console moved to that profile |
| `platform: no last-good copy ...` or `the last-good copy ... has no traefik.yml` | `--last-good` reads what the last successful `up` from the variables store kept. A copy from before Traefik's configuration became a file lacks `traefik.yml`: run one `platform.sh up` from the store first |
| `CLOUDFLARE_PROXIED is true but there is no <secrets>/cloudflare-ips` | Run `ygg install --role apps` as the admin: it downloads Cloudflare's ranges |
| `ygg check` says ssh `is there but sshd runs with ...` | Another file sets that SSH option before `00-yggdrasil.conf`: `sudo sshd -T` shows the winner, `grep -ri <option> /etc/ssh/sshd_config /etc/ssh/sshd_config.d`. See [hosts.md](hosts.md#etcsshsshd_configd00-yggdrasilconf) |
| The nightly backup fails (`journalctl --user -u yggdrasil-backup`, as `ygg`) | `missing backup.conf`, `BACKUP_TARGET is not set`, `no items in …`: [hosts.md, Backups](hosts.md#7-backups-and-restore). `Host key verification failed` or `Permission denied (publickey)`: the receiving machine's host key was not accepted once by hand, or its `authorized_keys` line is wrong |
| A **Build with Parameters → `DEPLOY_TO`** build waits forever | The `manual` environment has no `agent` in the catalog, so Jenkins has no node for it. Set `agent`, or deploy it by hand with `scripts/deploy.sh` |
| An application is `stopped` right after a Jenkins deploy | Expected in an on-demand environment that was stopped: the deploy checked the new version and stopped it again. `ygg env start <environment>` ([On-demand environments](#on-demand-environments)) |
| `YGG_ENVIRONMENT is set by scripts/deploy.sh` from `docker compose` | A stack's proxy overlay used without `deploy.sh`, which exports it. Deploy with `deploy.sh`, or export `YGG_ENVIRONMENT=<environment>` and `-p <application>-<environment>` |
| After upgrading from 0.4, Traefik routes an application to the old container, or a deploy fails on a name already in use | The project from before 0.5 (`<application>`, without the environment) still runs: `docker compose -p <application> down` ([CHANGELOG, Upgrading from 0.4 to 0.5](../CHANGELOG.md#upgrading-from-04-to-05)) |
| `deploy/<environment>` never appears on the pull request | The release environment's agent is offline, the pull request's `branch-policy` or a check failed (nothing is deployed then), or the build is waiting for the GitHub Actions checks or at the skip |
| Certificates, DNS, webhook deliveries | [dns.md troubleshooting](dns.md#troubleshooting) |
