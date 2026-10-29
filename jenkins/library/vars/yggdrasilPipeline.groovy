// The pipeline of every repository Jenkins builds. Every job runs the one Jenkinsfile kept in Jenkins
// (jenkins/Jenkinsfile.default, never the repository's own):
//
//     @Library('yggdrasil') _
//     yggdrasilPipeline()
//
// A repository's own Jenkinsfile is never read: a branch pushed by someone who is not the owner would
// otherwise run its own pipeline code with every credential in reach. What a repository says is data, its
// yggdrasil.yml, and this library -- loaded at the tag YGGDRASIL_LIBRARY_VERSION, a release of this
// repository -- decides what to do with it. docs/setup.md ("The pipeline") tells the same story for the
// operator, with the statuses, the skip and the accepted risks.
//
// The job's name is the catalog id (the Job DSL in platform/jenkins/controller/casc.yaml names each
// multibranch job after an application or a `repositories` entry); `stack:`, what older Jenkinsfiles
// passed, still wins when given. What a build deploys, and where, comes from catalog.yaml (mounted on
// the controller, the same file the Job DSL reads): the application's environments, each with its
// options resolved -- default, then the environment's value, then the application's override.
// docs/catalog.md has the options. What a build checks comes from the repository's yggdrasil.yml
// (data, never code: see scripts/pipeline_config.py), read on the ci agent before anything else.
//
//   pull request into develop or main (and master, under the basic rules), with yggdrasil.yml
//                            -> Branch policy (scripts/branch-policy.sh, then the file's branchPolicy)
//                               and Checks (each planned check in parallel on the ci agent), each
//                               reported as a commit status; the build fails once all have reported if
//                               any failed. Into develop on Forgejo with a GitHub mirror, the entry's
//                               actionsChecks are then copied from GitHub Actions (see
//                               copyActionsChecks)
//   PR release/x.y.z -> main -> (with yggdrasil.yml: branch policy and checks first, under the same
//                               per-repository lock) wait for the entry's actionsChecks on GitHub Actions
//                               (the skip when they don't finish: "GitHub Actions checks didn't finish.
//                               Deploy without them?", recorded in the log, the build's description and
//                               the deploy/<environment> descriptions, and the release then goes out on
//                               the git server only, "GitHub copy pending"), then deploy the head to
//                               each trigger: release environment in catalog order, setting
//                               deploy/<environment> (required on main) after each; then, on the publish
//                               agent, merge the pull request and tag vx.y.z; build the release files on
//                               the release agent (and the Windows ones on GitHub), validate them there
//                               (pass or fail only: validation cannot change what is uploaded), run
//                               release.finalize over them (it only adds files: a manifest, a
//                               signature), and publish the release with publish-release.sh -- drafts
//                               on the git server and, with a mirror, GitHub, the files uploaded and
//                               checked, then the git server's published and GitHub's last (see
//                               releasePullRequest). A failure after the merge says how to finish: the
//                               release-resume job. On GitHub, a repository without yggdrasil.yml keeps
//                               the path it always had (release(): every GitHub Actions check waited
//                               for, releaseArtifacts: true's artifact workflow)
//   the actions-release job  -> the Windows files of a release published without them (actionsRelease)
//   the tag-check job        -> daily: every published release's tag still on the commit it was
//                               released from, else the build fails naming it MOVED or DELETED
//                               (tagCheck: the read credential on the ci agent, nothing checked out)
//   a version tag pushed by the owner (a repositories entry with release: owner-tag, the tag matching
//                               its `tags`; the Job DSL discovers only tags at most a day old, and dates
//                               a lightweight tag by its commit, so the owner tags with `git tag -a`)
//                            -> the tag's commit checked to be on main, then, from its yggdrasil.yml,
//                               the release files built and validated, publish.nuget's packages packed
//                               and pushed (the NuGet key bound for the push only, on the release agent)
//                               and/or the release, as a release pull request makes it after its merge
//                               (tagBuild, releaseTag)
//   the release-resume job   -> the same for a tag that exists already: a release that failed after its
//                               merge, the GitHub copy of a release published after the skip, a push of
//                               packages that stopped half way (releaseResume)
//   push to develop, main or release/* (and master, under the basic rules), with yggdrasil.yml
//                            -> the checks, then the branch's deploys only if every check passed; a
//                               check named in deploy.inputs hands its archive to the image build
//   branch pushed            -> every environment with trigger: branch whose `branches` glob matches
//   "Build with Parameters"  -> DEPLOY_TO: one environment with trigger: manual or branch (on a
//                               discovered branch), whatever the branch would trigger; no checks
//   any other pull request   -> nothing
//
// A repository without yggdrasil.yml gets no branch policy and no checks: it deploys (and releases)
// exactly as before the file existed.
//
// An environment with approval: true waits for someone to click "Deploy" first, without holding an
// agent. Each environment deploys on its agent (`agent`, default its id), so a stage runs on the
// host it deploys to; environments sharing a host share its agent. Checks run on the `ci` agent, and a
// build never holds a deploy agent while they run.
//
// onDemand environments: a deploy a push triggers leaves a switched-off environment switched off
// (deploy.sh starts it, waits for health, and stops it again). A DEPLOY_TO build is someone asking
// to use the environment, so it runs deploy.sh with DEPLOY_START=1 and leaves it running.
//
// The work itself is in scripts/ of this repository, checked out next to the application (./app and
// ./yggdrasil in the workspace), so a deploy or a check by hand runs the same code: deploy.sh,
// branch-policy.sh, run-check.sh, pipeline_config.py, forge.sh (the git server's interface for Forgejo
// and GitHub alike), github.sh (GitHub Actions), publish-release.sh with release_notes.py (a release and
// its notes) and nuget-push.sh (a library's packages). YGG_GITHUB_SH, in an agent's environment (set by
// the live tests only), replaces scripts/github.sh.
//
// Credentials, the rules this file keeps (scripts/test_pipeline_library.py checks them):
//   - the forge's own credential ids are named in forgeCredential() only;
//   - the ci agent never binds the GitHub App: the GitHub Actions lookup runs on the publish agent, and
//     on GitHub the ci agent reads with github-read when it is configured;
//   - the write credential is bound by reportStatus(), which posts from the controller, outside any
//     node(), and by onPublishAgent(), on the publish agent (forge.publishAgent: merges, tags, releases,
//     uploads), the one agent that also gets the GitHub App for a release. Neither the ci nor the
//     release agent ever holds either: checkouts and reads there use the read credential, and the
//     release agent's builds and finalize steps only the ids release.build.credentials and
//     release.finalize.credentials name (allow-listed against the entry's `releaseCredentials`, never its
//     checks' `credentials`). Files cross between the agents as stashes;
//   - nothing waits on an agent: GitHub Actions, a Windows run, a tag reaching GitHub and the skip are
//     waited for outside node(), each look a short call on the publish agent (pollGithub);
//   - a check binds only the ids its plan names, each one checked against the catalog entry's
//     `credentials` again here (never its `releaseCredentials`), and never one of the forge's own nor
//     the NuGet key (withCheckCredentials);
//   - the NuGet key (nuget-api-key) is bound by withNugetKey() only, on the release agent, around
//     scripts/nuget-push.sh and nothing else: no repository code runs in that step (pushPackages);
//   - no value reaches a shell script through Groovy interpolation: every `sh` script is a single-
//     quoted literal, and values go in as environment variables (withEnv), quoted "$VAR" there.

import groovy.json.JsonOutput
import groovy.json.JsonSlurperClassic
import groovy.transform.Field
import org.jenkinsci.plugins.workflow.steps.FlowInterruptedException

@Field static final String CATALOG = '/etc/jenkins/catalog.yaml'
@Field static final String RELEASE_BRANCH = /^release\/(\d+\.\d+\.\d+)$/
// What the library accepts from outside before using it: a catalog id (the job's name), a commit, and
// the fields of a planned check (scripts/pipeline_config.py validates the same; this is a second look,
// next to where the values are used). All are full matches (==~).
@Field static final String CATALOG_ID = '[a-z0-9]+(-[a-z0-9]+)*'
@Field static final String COMMIT = '[0-9a-f]{40}'
@Field static final String CHECK_NAME = '[a-z0-9][a-z0-9._-]*'
@Field static final String ENV_VAR = '[A-Za-z_][A-Za-z0-9_]*'
@Field static final String IMAGE = '[A-Za-z0-9][A-Za-z0-9._/:@-]*'
// A status context: a check's name, branch-policy, deploy/<environment>, or a GitHub Actions check
// named in the catalog ("Console (Windows)").
@Field static final String STATUS_CONTEXT = '[A-Za-z0-9][A-Za-z0-9 ._()/-]{0,99}'
@Field static final String WEB_URL = 'https://[^\\s"\'<>]+'
// Seconds between two looks at GitHub Actions (checks and workflow runs), and at GitHub for a tag the
// mirror is pushing; how long a release tag gets to reach GitHub (publish-release.sh's GITHUB_TAG_WAIT
// default).
@Field static final int ACTIONS_POLL_SECONDS = 60
@Field static final int TAG_POLL_SECONDS = 10
@Field static final int TAG_WAIT_SECONDS = 600
// A tag of a release (v1.4.0, 1.4.0, or <package>@<version> for a library that versions each of its packages
// apart: ArturRios.Data.Sqlite@3.1.0), before it reaches git or the forge. `@` never first: git refuses a ref
// named `@`, and a tag must start with a letter or digit like every name the pipeline takes.
@Field static final String TAG = '[A-Za-z0-9][A-Za-z0-9._@-]{0,99}'
// The version the library was loaded at (a yggdrasil release tag; a branch name is accepted too), and
// what of this repository is the owner's data, always taken from main (checkoutTools).
@Field static final String LIBRARY_VERSION = '[A-Za-z0-9][A-Za-z0-9._/-]{0,99}'
@Field static final List OWNER_DATA = ['catalog.yaml', 'stacks']
// A release file glob (release.build.files, release.windows.files, release.finalize.files) as the stash
// step takes it: Ant patterns (*, **, ?), relative, with no comma or whitespace (stash splits its includes
// on them); a `.` or `..` segment is refused separately (releaseProblem).
@Field static final String RELEASE_GLOB = '[A-Za-z0-9_.*?+@=][A-Za-z0-9_.*?/+@=-]{0,199}'
// A GitHub workflow file (release.windows.workflow).
@Field static final String WORKFLOW = '[A-Za-z0-9][A-Za-z0-9._-]{0,99}\\.ya?ml'
// The skip (the design's words): asked when the GitHub Actions checks of a release did not finish.
@Field static final String SKIP_QUESTION = "GitHub Actions checks didn't finish. Deploy without them?"
@Field static final String SKIP_BUTTON = 'Deploy without GitHub Actions checks'
@Field static final String SKIPPED = 'without GitHub Actions checks'
// delete-branch exit 3: Forgejo keeps a protected branch, which is not a reason to fail a release.
@Field static final String KEPT_BRANCH = 'kept: release branches are protected on Forgejo; the owner deletes them'
// The NuGet API key's Jenkins credential (casc.nuget.yaml): a fixed id, since yggdrasil.yml's publish has no
// credentials field. Bound by withNugetKey() only.
@Field static final String NUGET_CREDENTIAL = 'nuget-api-key'
// publish.nuget.packages when the file does not say.
@Field static final String NUGET_PACKAGES = '**/*.nupkg'
// What the tag-check job archives each run, and reads back from the run before: the releases it knows of.
@Field static final String TAG_RECORD = 'release-tags.tsv'

// Run from a directory: exit 3, naming them, when a symbolic link under it leads out of it -- or, with
// YGG_ANY_LINK set, when there is any link at all (a release's files are files). A build or a check ran as
// root and can leave a link to anything on the agent (its own files, another workspace, @tmp with the env
// files); archiveArtifacts can be told not to follow links, but junit, stash and the uploads cannot, so
// this runs before them. .git is pruned (the Ant default excludes keep it out of every stash anyway).
// Flutter's pub get links each desktop plugin from the pub cache, outside the checkout, under
// <platform>/flutter/ephemeral/.plugin_symlinks: generated, git-ignored and never collected, so those
// directories are removed first (rm removes the links, never what they point at). A .plugin_symlinks that is
// itself a link is no directory, stays, and is refused like any other.
@Field static final String LINK_GUARD = '''
root=$(pwd -P)
export root
if [ -n "${YGG_ANY_LINK:-}" ]; then
  links=$(find . -type l | head -n 20)
  if [ -n "$links" ]; then
    echo "Links among the files:" >&2
    echo "$links" >&2
    exit 3
  fi
  exit 0
fi
find . -path ./.git -prune -o -type d -path '*/flutter/ephemeral/.plugin_symlinks' -prune -exec rm -rf {} +
outside=$(find . -path ./.git -prune -o -type l -exec sh -c '
  for link; do
    case "$(realpath -m -- "$link")" in
      "$root" | "$root"/*) ;;
      *) echo "$link" ;;
    esac
  done' sh {} +)
if [ -n "$outside" ]; then
  echo "Links that lead out of the checkout:" >&2
  echo "$outside" | head -n 20 >&2
  exit 3
fi
'''

// Run from a directory: every path under it with its type (`f ./a`, `d ./b`, `l ./c`...), sorted, then
// every regular file's SHA-256 (sha256sum's lines). Two runs print the same only when nothing was changed,
// added, removed or replaced by a link there: what release.finalize may not do to the release's files
// (finalizeReleaseFiles). A file that cannot be read fails it.
@Field static final String RELEASE_SUMS = '''
set -eu
(set -o pipefail) 2>/dev/null && set -o pipefail
export LC_ALL=C
find . -mindepth 1 -printf '%y %p\n' | sort
find . -type f -print0 | sort -z | xargs -0 -r sha256sum --
'''

// Run from a directory: the file names (base names, not paths) that more than one file under it has, each
// ended by a NUL (a name may hold any other character, a newline too), sorted; nothing when every name is
// unique. A release shows one file per name (publish-release.sh uploads each by its name), so the upload set
// must have no two (refuseDuplicateNames, which names at most 20). bash, for pipefail: a find that fails
// fails the check instead of passing it with what it read so far.
@Field static final String DUPLICATE_NAMES = '''#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C
find . -type f -printf '%f\\0' | sort -z | uniq -zd
'''

// Runs one of this repository's scripts, yggdrasil/scripts/<YGG_TOOL>, from the workspace's root with
// the arguments YGG_ARG_1 .. YGG_ARG_<YGG_ARGC> (see tool()): the values are environment variables,
// never part of the script, so no argument can change what the shell runs. With YGG_OUT set, stdout
// goes to that file. github.sh is $YGG_GITHUB_SH instead when the agent sets it (the live tests).
@Field static final String TOOL = '''#!/usr/bin/env bash
set -euo pipefail
args=()
for ((i = 1; i <= YGG_ARGC; i++)); do
  name="YGG_ARG_$i"
  args+=("${!name}")
done
if [[ -n "${YGG_OUT:-}" ]]; then
  mkdir -p "$(dirname "$YGG_OUT")"
  exec >"$YGG_OUT"
fi
if [[ "$YGG_TOOL" == github.sh && -n "${YGG_GITHUB_SH:-}" ]]; then
  exec "$YGG_GITHUB_SH" "${args[@]}"
fi
exec bash "yggdrasil/scripts/$YGG_TOOL" "${args[@]}"
'''

def call(Map config = [:]) {
  // JOB_NAME is <job>/<branch or PR-n>: the multibranch job's own name is the catalog id.
  Map ctx = catalogContext(config.stack ?: env.JOB_NAME.tokenize('/')[0])
  String app = ctx.app
  List plan = ctx.plan
  boolean releaseArtifacts = ctx.releaseArtifacts

  // Not release environments: those only ever run the head of a release pull request.
  List byHand = plan.findAll { it.trigger in ['manual', 'branch'] }.collect { it.id }
  // A newer push to a pull request that is not a release replaces the build still checking the older
  // commit (it may be waiting up to checksTimeout for GitHub Actions); every other build waits its
  // turn, a release above all.
  boolean replaceable = env.CHANGE_ID && env.CHANGE_TARGET != 'main'
  properties([
    disableConcurrentBuilds(abortPrevious: replaceable),
    buildDiscarder(logRotator(numToKeepStr: '50')),
    parameters([
      choice(name: 'DEPLOY_TO', choices: [''] + byHand,
             description: 'Deploy this branch to an environment with trigger: manual or branch, and leave it running if it is on demand. Empty: only what the branch triggers.'),
    ]),
  ])

  timeout(time: 4, unit: 'HOURS') {
    withForgeEnvironment(ctx) {
      // A tag build: TAG_NAME is set for a tag head (what buildingTag() reads).
      if (env.TAG_NAME) {
        tagBuild(ctx)
        return
      }
      if (env.CHANGE_ID) {
        pullRequest(ctx, plan, releaseArtifacts)
        return
      }
      branchBuild(ctx, plan)
    }
  }
}

// What the catalog says about a job: [app, repo (its git repository), forge, mirror, checksTimeout (of
// its first environment), plan (its environments, resolved), releaseArtifacts, release (deploy: an
// application with a trigger: release environment; owner-tag; none -- pipeline_config.py entry_for's
// reading), tags (its version tag pattern), rules (its git repository's model, release or basic:
// repositoryModel), owner, repository (this repository's name)]. Fails unless `app` is a catalog id of an
// application or a repositories entry.
Map catalogContext(String app) {
  if (!(app ==~ CATALOG_ID)) {
    error "'${printable(app)}' is not a catalog id: the job must be named after an application or a repositories entry"
  }
  Map catalog = loadCatalog()
  Map application = findApplication(catalog, app)
  Map repositoryEntry = application == null ? findRepositoryEntry(catalog, app) : null
  if (application == null && repositoryEntry == null) {
    error "'${app}' is not an application or a repositories entry in ${CATALOG}"
  }
  // A repositories entry (a library, yggdrasil itself) has no environments: it never deploys.
  List plan = application != null ? resolve(catalog, app) : []
  String repo = application != null ? (application.repository ?: app) : app
  return [app: app, repo: repo, forge: forgeOf(catalog), rules: repositoryModel(catalog, repo),
          mirror: mirrorOf(catalog), checksTimeout: (plan ? plan[0].checksTimeout : 3600) as int, plan: plan,
          releaseArtifacts: application?.releaseArtifacts == true,
          release: application != null ? (plan.any { it.trigger == 'release' } ? 'deploy' : 'none')
                                        : (repositoryEntry.release ?: 'none').toString(),
          tags: application != null ? 'v*' : (repositoryEntry.tags ?: 'v*').toString(),
          owner: catalog.owner.toString(), repository: (catalog.repository ?: 'yggdrasil').toString()]
}

// forge.sh reads the git server from these (and the token each call binds), so an agent needs no
// catalog of its own to talk to it.
def withForgeEnvironment(Map ctx, Closure body) {
  withEnv(["GITHUB_OWNER=${ctx.owner}", "YGGDRASIL_REPOSITORY=${ctx.repository}", "FORGE_TYPE=${ctx.forge.type}",
           "FORGE_URL=${ctx.forge.url}", "FORGE_API_URL=${ctx.forge.api}", "FORGE_OWNER=${ctx.owner}"]) {
    body()
  }
}

// ---- Pull requests ----

void pullRequest(Map ctx, List plan, boolean releaseArtifacts) {
  String app = ctx.app
  String target = env.CHANGE_TARGET
  if (target == 'main' && (env.CHANGE_BRANCH ?: '') ==~ RELEASE_BRANCH) {
    // A multibranch job's disableConcurrentBuilds is branch-local. This application-wide lock covers
    // checks, every promotion, merge, packaging and publication, so two release branches cannot race
    // through production or publish out of deployment order.
    lock(resource: "release-${app}") {
      List environments = plan.findAll { it.trigger == 'release' }
      // Only the job with a release environment releases. It deploys production before it merges, so it
      // runs every check main requires of the repository (each catalog entry's, not only its own), then
      // deploy.inputs, whose archives are stashed. The other jobs of the repository (a library, a docs site
      // without a release environment) build the same pull request with their own checks alone.
      boolean releasing = environments as boolean
      Map state = prepare(ctx, 'pull-request', releasing, releasing)
      if (state.config != null) {
        List failed = verify(ctx, state)
        if (failed) {
          error releasing ? "Not releasing: ${failed.join(', ')} failed."
                          : "Failed: ${failed.join(', ')}."
        }
      }
      if (ctx.forge.type == 'github' && state.config == null) {
        // A GitHub installation's repository without yggdrasil.yml releases exactly as it always did.
        release(app, environments, releaseArtifacts, ctx.repo)
      } else {
        releasePullRequest(ctx, state, environments)
      }
    }
    return
  }
  if (!(target in checkedBranches(ctx.rules))) {
    echo "Nothing to deploy for a pull request from ${printable(env.CHANGE_BRANCH)} into ${printable(target)}."
    return
  }
  Map state = prepare(ctx, 'pull-request')
  if (state.config == null) {
    echo "Nothing to deploy for a pull request from ${printable(env.CHANGE_BRANCH)} into ${target} (no yggdrasil.yml: no branch policy, no checks)."
    return
  }
  List failed = verify(ctx, state)
  if (failed) {
    // GitHub Actions is not waited for: the pull request cannot be merged anyway, and the next push
    // starts a new build.
    error "Failed: ${failed.join(', ')}."
  }
  if (target == 'develop' && ctx.forge.type == 'forgejo' && ctx.mirror != null && state.entry.actionsChecks) {
    copyActionsChecks(ctx, state)
  }
}

// The branch policy, then the checks, each reported as it finishes. Returns the contexts that
// failed (branch-policy and check names), after all of them have reported.
List verify(Map ctx, Map state) {
  List failed = []
  if (!branchPolicy(ctx, state)) {
    failed << 'branch-policy'
  }
  failed.addAll(runChecks(ctx, state))
  return failed
}

// ---- Pushes ----

void branchBuild(Map ctx, List plan) {
  String app = ctx.app
  String branch = env.BRANCH_NAME
  boolean deployToParameter = params.DEPLOY_TO as boolean
  List targets = deployToParameter
    ? plan.findAll { it.id == params.DEPLOY_TO && it.trigger in ['manual', 'branch'] }
    : plan.findAll { it.trigger == 'branch' && matchesAny(it.branches, branch) }

  List inputs = []
  // The commit the checks ran on (and found no yggdrasil.yml on): what must be deployed. null when
  // nothing was prepared (other branches, DEPLOY_TO), which deploys the checkout as it always did.
  String checkedSha = null
  // "Build with Parameters" deploys without checks, as it always did.
  if (!deployToParameter && (branch in checkedBranches(ctx.rules) || branch.startsWith('release/'))) {
    Map state = prepare(ctx, 'push', targets as boolean)
    checkedSha = state.sha
    if (state.config != null) {
      List failed = runChecks(ctx, state)
      if (failed) {
        error "Failed: ${failed.join(', ')}. Nothing is deployed."
      }
      // The next push compares its changes with this commit (lastCheckedCommit).
      env.YGG_CHECKED_COMMIT = state.sha
      inputs = deployInputs(state.config)
    }
  }

  if (!targets) {
    echo "Nothing to deploy for ${printable(branch)}: no environment of ${app} is triggered by it (catalog.yaml)."
    return
  }
  String version = (branch ==~ RELEASE_BRANCH) ? (branch =~ RELEASE_BRANCH)[0][1] : branch.replaceAll(/[^A-Za-z0-9_.-]/, '-')
  for (Map environment : targets) {
    deployTo(app, environment, version, null, deployToParameter, ctx.repo, inputs, checkedSha)
  }
}

// ---- yggdrasil.yml, the plan ----

// On the ci agent: checks out the commit, describes the repository (pipeline_config.py describe) and,
// for a pull request into develop or main or a push to develop, main or release/* (master too, under the
// basic rules: checkedBranches), plans its checks.
// Returns [sha, entry, config (null without yggdrasil.yml), plan (null when not planned)]. A file the
// catalog does not allow (an unknown field, a credential not in the entry's `credentials`, ...) fails
// the build here, before anything of it runs.
//
// mode: 'pull-request' (changes against the target branch) or 'push' (against the last commit whose
// checks passed). deploying: the build deploys afterwards, so the checks named in deploy.inputs run
// even when they could be reused or skipped -- their archive goes into the image. releasing: a release
// pull request of the job that releases, which runs every check main requires of the repository
// (describe's repositoryChecks) instead of its own entry's.
Map prepare(Map ctx, String mode, boolean deploying = false, boolean releasing = false) {
  // The build's commit, known before any checkout: every ci step fetches exactly this one.
  Map state = [sha: buildCommit()]
  stage('Read yggdrasil.yml') {
    node('ci') {
      try {
        checkoutCommit(ctx, state.sha)
        Map document = describeCheckout(ctx)
        state.entry = document.entry
        state.config = document.config
        state.repositoryChecks = document.repositoryChecks
        if (state.config != null) {
          String tmp = scratch()
          withEnv(["YGG_TMP=${tmp}", "YGG_JOB=${ctx.app}", "YGG_CATALOG=${tmp}/catalog.yaml"]) {
            state.plan = planChecks(ctx, state, mode, deploying, releasing)
          }
        }
      } finally {
        cleanScratch()
        cleanWs()
      }
    }
  }
  return state
}

// On an agent, with the repository checked out in ./app (the ci agent for a build's checks, the release
// agent for actions-release): pipeline_config.py describe against the controller's catalog -- the one
// the job was created from, not the checkout's copy, written to the scratch directory as catalog.yaml,
// where planChecks reads it too. Returns the document ([entry, config]); a file the catalog does not
// allow fails the build.
Map describeCheckout(Map ctx) {
  String tmp = scratch()
  dir(tmp) {
    writeFile file: 'catalog.yaml', text: catalogText()
  }
  Map described = null
  withEnv(["YGG_TMP=${tmp}", "YGG_JOB=${ctx.app}", "YGG_CATALOG=${tmp}/catalog.yaml"]) {
    int code = sh(script: '''
      python3 yggdrasil/scripts/pipeline_config.py describe "$YGG_JOB" app >"$YGG_TMP/describe.json"
    ''', returnStatus: true)
    String text = readScratch(tmp, 'describe.json')
    // describe exits 0 with a usable document, or 1 with the document's `errors`. Anything else (a crash
    // with half a document, no document, exit 1 and no errors) is a failure: reading it as "no
    // yggdrasil.yml" would let a broken describe turn every check off.
    def document = text.trim() ? parseJson(text) : null
    if (!(document instanceof Map) || (code != 0 && !document.errors)) {
      error "pipeline_config.py describe failed (exit ${code}); its message is above."
    }
    if (document.errors) {
      error "yggdrasil.yml cannot be used:\n${bulleted(document.errors as List)}"
    }
    // Allowed but likely a mistake (a check no catalog entry of the repository runs): said, and the build
    // marked UNSTABLE so it shows, never fatal.
    for (Object warning : (document.warnings ?: [])) {
      echo "Warning: ${printable(warning)}"
    }
    if (document.warnings) {
      unstable("yggdrasil.yml: ${(document.warnings as List).size()} warning(s), above")
    }
    described = [entry: document.entry, config: document.config, repositoryChecks: document.repositoryChecks]
  }
  return described
}

// Inside prepare(): the commit's statuses (who already checked what) and the changed files, then
// pipeline_config.py plan (--deploying when the build deploys). Returns the plan, checked again here
// (planProblem, planCoverage). The plan holds this job's checks only: its catalog entry's `checks`, and
// deploy.inputs when it deploys. yggdrasil.yml serves every catalog entry of the repository (a library and
// its docs site), so its other checks are another job's: they neither run nor report here.
List planChecks(Map ctx, Map state, String mode, boolean deploying, boolean releasing = false) {
  String diffMode = 'none'
  String diffBase = ''
  String branch
  if (mode == 'pull-request') {
    // CHANGE_BRANCH, not BRANCH_NAME (PR-n): a renovate/* branch gets no credentials.
    branch = env.CHANGE_BRANCH
    diffMode = 'pull-request'
    diffBase = env.CHANGE_TARGET
  } else {
    branch = env.BRANCH_NAME
    String base = lastCheckedCommit()
    if (base) {
      diffMode = 'push'
      diffBase = base
    }
  }
  withEnv(["YGG_REPO=${ctx.repo}", "YGG_SHA=${state.sha}", "YGG_DIFF_MODE=${diffMode}", "YGG_DIFF_BASE=${diffBase}",
           "YGG_BRANCH=${branch}", "YGG_DEPLOYING=${deploying ? '1' : ''}", "YGG_RELEASING=${releasing ? '1' : ''}"]) {
    withForgeToken('read') {
      // Without the statuses nothing is reused: every check runs, which is the safe way to be wrong.
      sh(script: '''
        if ! bash yggdrasil/scripts/forge.sh get-statuses "$YGG_REPO" "$YGG_SHA" >"$YGG_TMP/statuses"; then
          echo "Could not read the statuses of $YGG_SHA: every check runs." >&2
          : >"$YGG_TMP/statuses"
        fi
      ''')
    }
    // "-" tells plan the changes are unknown (every check runs): never an empty list for a diff that
    // failed, which would skip every check with paths.
    int code = sh(script: '''
      changed="$YGG_TMP/changed"
      case "$YGG_DIFF_MODE" in
        pull-request) git -C app diff --name-only "origin/$YGG_DIFF_BASE...HEAD" >"$changed" || changed=- ;;
        push) git -C app diff --name-only "$YGG_DIFF_BASE" HEAD >"$changed" || changed=- ;;
        *) changed=- ;;
      esac
      if [ "$changed" = - ]; then
        echo "The changed files are not known: every check runs." >&2
      fi
      set --
      if [ -n "$YGG_DEPLOYING" ]; then set -- "$@" --deploying; fi
      if [ -n "$YGG_RELEASING" ]; then set -- "$@" --release; fi
      python3 yggdrasil/scripts/pipeline_config.py plan "$YGG_JOB" app --statuses "$YGG_TMP/statuses" \\
        --changed "$changed" --branch "$YGG_BRANCH" "$@" >"$YGG_TMP/plan.json"
    ''', returnStatus: true)
    if (code != 0) {
      error "pipeline_config.py plan failed (exit ${code}); its message is above."
    }
  }
  List planned = parseJson(readScratch(scratch(), 'plan.json')) as List
  List wanted = neededChecks(state.entry, state.config, deploying,
                             releasing ? state.repositoryChecks as List : null)
  String problem = planProblem(planned, state.entry) ?: planCoverage(planned, wanted)
  if (problem) {
    error "The plan of the checks is not usable: ${problem}"
  }
  String others = notNeeded(state.config, planned)
  if (others) {
    echo "Not run by ${ctx.app} (not its catalog checks): ${others}"
  }
  if (deploying) {
    List inputs = deployInputs(state.config)
    for (Map check : planned) {
      if (check.name in inputs && check.action != 'run') {
        check.action = 'run'
        check.reason = 'run: a deploy input, its archive goes into the image'
      }
    }
  }
  for (Map check : planned) {
    echo "Check ${check.name}: ${check.action} (${printable(check.reason)})"
  }
  return planned
}

// The commit of the last successful build of this branch that passed its checks (it set
// YGG_CHECKED_COMMIT), or null: a push's changes are counted from there. UNSTABLE counts as successful:
// it is how describe's warnings show, after checks that passed. Looks back 20 successful builds (among
// 100 at most); a build that only deployed (DEPLOY_TO, no yggdrasil.yml yet) has no commit and is passed
// over.
String lastCheckedCommit() {
  def build = currentBuild.previousBuild
  int looked = 0
  for (int seen = 0; build != null && seen < 100 && looked < 20; seen++) {
    if (build.result in ['SUCCESS', 'UNSTABLE']) {
      looked++
      String sha = build.buildVariables?.YGG_CHECKED_COMMIT
      if (sha && sha ==~ COMMIT) {
        return sha
      }
    }
    build = build.previousBuild
  }
  return null
}

// ---- Branch policy ----

// Stage Branch policy, on a pull request into develop or main: scripts/branch-policy.sh, then the
// file's own branchPolicy (in its image, with HEAD_REF, BASE_REF, HEAD_SHA and YGGDRASIL_DIR, the
// yggdrasil checkout's path, in its environment). Reported as branch-policy, never reused. Returns
// whether it passed. A repository under the `basic` rules (describe's entry.rules: a site deployed on pushes
// to main, a fork) has no release model to follow: it passes for any pair of refs, and neither the script
// nor the file's own branchPolicy runs.
boolean branchPolicy(Map ctx, Map state) {
  boolean passed = false
  String reason = 'The branch policy could not run; see the build'
  if (state.entry?.rules == 'basic') {
    stage('Branch policy') {
      echo 'Branch policy: the repository has the basic rules, so there is no branching model to check.'
      reportStatus(ctx.repo, state.sha, 'success', 'branch-policy', 'basic: nothing to check')
    }
    return true
  }
  stage('Branch policy') {
    reportStatus(ctx.repo, state.sha, 'pending', 'branch-policy', 'Checking the branching model')
    try {
      node('ci') {
        try {
          checkoutCommit(ctx, state.sha)
          String tmp = scratch()
          withEnv(["YGG_TMP=${tmp}", "YGG_HEAD_REF=${env.CHANGE_BRANCH}", "YGG_BASE_REF=${env.CHANGE_TARGET}",
                   "YGG_HEAD_SHA=${state.sha}"]) {
            int code = sh(script: '''
              mkdir -p "$YGG_TMP"
              cd app
              bash ../yggdrasil/scripts/branch-policy.sh "$YGG_HEAD_REF" "$YGG_BASE_REF" "$YGG_HEAD_SHA" >"$YGG_TMP/branch-policy.out" 2>&1
            ''', returnStatus: true)
            String said = readScratch(tmp, 'branch-policy.out').trim()
            echo printable(said)
            passed = code == 0
            reason = passed ? 'Follows the branching model' : (lastLine(said) ?: "branch-policy.sh exited ${code}")
            Map extra = state.config.branchPolicy as Map
            if (passed && extra) {
              withEnv(["YGG_IMAGE=${extra.image}", "YGG_RUN=${extra.run}"]) {
                code = sh(script: '''
                  umask 077
                  env_file="$YGG_TMP/branch-policy.env"
                  {
                    printf 'HEAD_REF=%s\\n' "$YGG_HEAD_REF"
                    printf 'BASE_REF=%s\\n' "$YGG_BASE_REF"
                    printf 'HEAD_SHA=%s\\n' "$YGG_HEAD_SHA"
                    printf 'YGGDRASIL_DIR=%s\\n' "$WORKSPACE/yggdrasil"
                  } >"$env_file"
                  # The whole workspace is mounted (the yggdrasil checkout next to the application's);
                  # the command runs in the application's root, as every check does.
                  bash yggdrasil/scripts/run-check.sh branch-policy "$YGG_IMAGE" "$WORKSPACE" --env-file "$env_file" \\
                    -- sh -c 'cd app && exec sh -c "$1"' sh "$YGG_RUN"
                ''', returnStatus: true)
              }
              passed = code == 0
              if (!passed) {
                reason = "The repository's own branch policy failed (exit ${code})"
              }
            }
          }
        } finally {
          cleanScratch()
          cleanWs()
        }
      }
    } catch (FlowInterruptedException interruption) {
      throw interruption
    } catch (Exception problem) {
      passed = false
      reason = "The branch policy could not run: ${problem.message}"
      echo printable(reason)
    }
    reportStatus(ctx.repo, state.sha, passed ? 'success' : 'failure', 'branch-policy', reason)
  }
  return passed
}

// ---- Checks ----

// Stage Checks: every planned check, reused and skipped ones as a success status with the reason,
// the rest run in parallel, one ci executor each. Returns the names of the checks that failed, once
// all have reported.
List runChecks(Map ctx, Map state) {
  List failed = []
  stage('Checks') {
    if (!state.plan) {
      echo 'Nothing to check: the catalog entry of this job names no checks, and this build needs no deploy.inputs.'
      return
    }
    Map branches = [:]
    for (Map check : state.plan) {
      Map planned = check
      if (planned.action != 'run') {
        reportStatus(ctx.repo, state.sha, 'success', planned.name, planned.reason)
        continue
      }
      reportStatus(ctx.repo, state.sha, 'pending', planned.name, 'Running in Jenkins')
      branches[planned.name] = {
        if (!runCheck(ctx, state, planned)) {
          failed << planned.name
        }
      }
    }
    if (branches) {
      parallel branches
    }
  }
  return failed.sort()
}

// One check on its own ci executor: scripts/run-check.sh in the check's image, with its allow-listed
// credentials in an env file; its junit results and archive collected (the archive also stashed as
// check-<name>, for deploy.inputs), whatever the outcome. The status is posted afterwards, outside
// the node. Returns whether it passed.
boolean runCheck(Map ctx, Map state, Map check) {
  int code = -1
  String problem = null
  try {
    node('ci') {
      try {
        checkoutCommit(ctx, state.sha)
        withCheckCredentials(check, state.entry, 'credentials') { String envFile ->
          withEnv(["YGG_CHECK_NAME=${check.name}", "YGG_CHECK_IMAGE=${check.image}", "YGG_CHECK_RUN=${check.run}",
                   "YGG_CHECK_DOCKER=${check.docker == true ? '1' : ''}", "YGG_CHECK_ENV_FILE=${envFile}"]) {
            // run-check.sh takes the command in exec form: sh -c runs the file's `run` line as written.
            code = sh(script: '''
              set --
              if [ -n "$YGG_CHECK_DOCKER" ]; then set -- "$@" --docker; fi
              if [ -n "$YGG_CHECK_ENV_FILE" ]; then set -- "$@" --env-file "$YGG_CHECK_ENV_FILE"; fi
              bash yggdrasil/scripts/run-check.sh "$YGG_CHECK_NAME" "$YGG_CHECK_IMAGE" "$WORKSPACE/app" "$@" \\
                -- sh -c "$YGG_CHECK_RUN"
            ''', returnStatus: true)
          }
        }
      } finally {
        collectResults(check)
        cleanScratch()
        cleanWs()
      }
    }
  } catch (FlowInterruptedException interruption) {
    throw interruption
  } catch (Exception failure) {
    problem = failure.message
    echo "Check ${check.name} could not run: ${printable(problem)}"
  }
  boolean passed = code == 0 && problem == null
  String description = passed ? 'Passed in Jenkins'
    : problem != null ? "Could not run: ${problem}" : "Failed in Jenkins (exit ${code})"
  reportStatus(ctx.repo, state.sha, passed ? 'success' : 'failure', check.name, description)
  if (!passed) {
    // Its sh returned the status, so nothing failed this parallel branch and Jenkins drew it green. Marked
    // failed here; the build's result is left alone, so the other checks run on and verify() fails the build
    // once all have reported.
    catchError(buildResult: 'SUCCESS', stageResult: 'FAILURE') {
      error("${check.name}: ${printable(description)}")
    }
  }
  return passed
}

// A check's credentials ({ENV: credential id}, from its plan) bound for the body only, written to a
// file only the agent's user can read (umask 077, in the workspace's @tmp, outside every mount), handed
// to the body as its path ('' when the check has none) and deleted afterwards. Every id is checked
// against the catalog entry's list for the caller's context here, right before it is bound: `allowList`
// is 'credentials' for a check (runCheck) and 'releaseCredentials' for the release build
// (buildReleaseFiles). The two lists are kept apart in the catalog because a check runs code from any
// branch: a signing key allowed for the release build is never bindable by a check. The forge's own ids
// (any id starting with forge- or github-) and the NuGet key are refused whatever the catalog says.
void withCheckCredentials(Map check, Map entry, String allowList, Closure body) {
  if (!(allowList in ['credentials', 'releaseCredentials'])) {
    error "withCheckCredentials: unknown allow-list '${printable(allowList)}'"
  }
  Map credentials = (check.credentials ?: [:]) as Map
  if (credentials.isEmpty()) {
    body('')
    return
  }
  List allowed = (entry[allowList] ?: []) as List
  // No branch at all in the release-resume job (a plain pipeline job): it releases a tag on main.
  String branch = env.CHANGE_BRANCH ?: env.BRANCH_NAME ?: ''
  if (branch.startsWith('renovate/')) {
    error "Check ${check.name}: a renovate/* build never gets credentials"
  }
  List variables = new ArrayList(credentials.keySet())
  List bindings = []
  for (int i = 0; i < variables.size(); i++) {
    String variable = variables[i]
    String id = credentials[variable]
    if (!(variable ==~ ENV_VAR)) {
      error "Check ${check.name}: '${printable(variable)}' is not an environment variable name"
    }
    if (!(id in allowed)) {
      error "Check ${check.name}: the credential '${printable(id)}' is not in the catalog's ${allowList} of ${entry.id}"
    }
    // The pipeline's own: forge-read, forge-write, forge-webhook, github-app, github-read.
    if (id.startsWith('forge-') || id.startsWith('github-')) {
      error "Check ${check.name}: '${id}' is named like one of the pipeline's own credentials (forge-*, github-*), never a check's"
    }
    // The NuGet key is the push step's alone (withNugetKey), where no repository code runs.
    if (id == NUGET_CREDENTIAL) {
      error "Check ${check.name}: '${id}' is the NuGet key, bound only to push a library's packages, never a check's"
    }
    bindings << string(credentialsId: id, variable: variable)
  }
  String envFile = "${scratch()}/check-${check.name}.env"
  withCredentials(bindings) {
    withEnv(["YGG_ENV_FILE=${envFile}", "YGG_ENV_NAMES=${variables.join(' ')}"]) {
      try {
        // No tracing: the values would be masked in the log, but they need not be there at all. A
        // value on several lines cannot be one line of an env file (docker reads NAME=value lines).
        sh(script: '''
          set +x
          umask 077
          mkdir -p "$(dirname "$YGG_ENV_FILE")"
          : >"$YGG_ENV_FILE"
          for name in $YGG_ENV_NAMES; do
            value=$(printenv "$name")
            case "$value" in
              *"
"*) echo "The credential bound to $name has more than one line; a check's env file cannot carry it." >&2; exit 1 ;;
            esac
            printf '%s=%s\\n' "$name" "$value" >>"$YGG_ENV_FILE"
          done
        ''')
        body(envFile)
      } finally {
        sh(script: 'rm -f "$YGG_ENV_FILE"')
      }
    }
  }
}

// junit results and the archive of a check, from the application's checkout. A failure to collect
// them makes the build unstable; it does not change the check's own result.
//
// The check wrote into that checkout as root, so it can leave a symbolic link to anything on the agent
// (its own files, another workspace, @tmp with the env files) named like a result. archiveArtifacts
// is told not to follow links, and junit and stash have no such switch: so before anything is
// collected, a link in the checkout that resolves outside it refuses the whole collection (LINK_GUARD).
void collectResults(Map check) {
  if (!check.junit && !check.archive) {
    return
  }
  try {
    dir('app') {
      int links = sh(script: LINK_GUARD, returnStatus: true)
      if (links != 0) {
        error "a link in the checkout leads outside it (exit ${links}); nothing of ${check.name} is collected"
      }
      if (check.junit) {
        junit testResults: check.junit, allowEmptyResults: true
      }
      if (check.archive) {
        archiveArtifacts artifacts: check.archive, allowEmptyArchive: true, followSymlinks: false
        stash name: "check-${check.name}", includes: check.archive, allowEmpty: true
      }
    }
  } catch (FlowInterruptedException interruption) {
    throw interruption
  } catch (Exception problem) {
    unstable "Could not collect the results of ${check.name}: ${printable(problem.message)}"
  }
}

// ---- GitHub Actions ----

// On Forgejo with a GitHub mirror, a pull request into develop also needs the entry's actionsChecks,
// the Windows and macOS jobs GitHub Actions runs on the mirror. Each is set pending, then looked at
// once a minute (pollGithub: each look on the publish agent, never ci -- the GitHub App's token must
// not sit next to the repositories' check code -- the wait between looks outside node(), so waiting
// never holds an agent) until github.sh actions-status says they all passed (0) or one failed (1), or
// checksTimeout runs out. Then each is copied to the forge with the GitHub run as its target. Not
// finished in time (still running, or GitHub not answering: exit 2 or 3): pending, "GitHub Actions
// didn't answer", and the build ends unstable; a rebuild looks again.
void copyActionsChecks(Map ctx, Map state) {
  List names = state.entry.actionsChecks as List
  requirePublishAgent(ctx)
  stage('GitHub Actions') {
    for (String name : names) {
      reportStatus(ctx.repo, state.sha, 'pending', name, 'Waiting for GitHub Actions')
    }
    Map look = pollGithub(ctx, ctx.checksTimeout, 'GitHub Actions', ['actions-status', ctx.repo, state.sha] + names, [0, 1],
                          ACTIONS_POLL_SECONDS)
    boolean finished = look.finished
    Map seen = actionsResults(look.out, names)
    for (String name : names) {
      Map result = seen[name] as Map
      if (result != null && result.state in ['success', 'failure']) {
        reportStatus(ctx.repo, state.sha, result.state, name, "GitHub Actions: ${result.state}", result.url)
      } else {
        reportStatus(ctx.repo, state.sha, 'pending', name,
                     finished ? 'GitHub Actions: still running' : "GitHub Actions didn't answer", result?.url)
      }
    }
    if (look.code == 1) {
      error "GitHub Actions: a check failed (${names.join(', ')})."
    }
    if (!finished) {
      unstable "GitHub Actions didn't answer within ${ctx.checksTimeout} s: ${names.join(', ')} left pending."
    }
  }
}

// Looks at GitHub with `github.sh <arguments>` until it exits with one of `done` or `seconds` run out:
// each look a short call on the publish agent (githubLook), the wait between two looks (`every`
// seconds, less when the time is nearly up) outside node(), so a wait of an hour holds no executor.
// Returns the last look, [code, out], with finished: whether its code is one of `done`.
Map pollGithub(Map ctx, int seconds, String what, List arguments, List done, int every) {
  long deadline = System.currentTimeMillis() + seconds * 1000L
  Map look = githubLook(ctx, arguments)
  while (!(look.code in done) && System.currentTimeMillis() < deadline) {
    long left = (deadline - System.currentTimeMillis()).intdiv(1000L) + 1
    int wait = Math.max(1L, Math.min((long) every, left)) as int
    echo "${what}: not there yet (github.sh ${arguments[0]} exit ${look.code}); looking again in ${wait} s."
    sleep time: wait, unit: 'SECONDS'
    look = githubLook(ctx, arguments)
  }
  look.finished = look.code in done
  return look
}

// One call of github.sh on the publish agent (forge.publishAgent: the agent that holds the GitHub App
// for releases), with the App's token, against the repositories' GitHub owner (githubOwner). Returns
// [code: its exit, out: its stdout].
Map githubLook(Map ctx, List arguments) {
  return onPublishAgent(ctx, false, true) {
    String tmp = scratch()
    int code = 2
    withEnv(["GITHUB_OWNER=${githubOwner(ctx)}"]) {
      code = toolStatus('github.sh', arguments, "${tmp}/github.out")
    }
    return [code: code, out: readScratch(tmp, 'github.out')]
  }
}

// Where a repository's GitHub Actions run: on Forgejo, its push mirror (owner mirror.github, the same
// repository name); on GitHub, the repository itself.
String githubOwner(Map ctx) {
  return ctx.mirror != null ? ctx.mirror.github : env.GITHUB_OWNER
}

// Whether the repositories have GitHub Actions at all: GitHub itself, or Forgejo with a mirror.
boolean githubActions(Map ctx) {
  return ctx.forge.type == 'github' || ctx.mirror != null
}

// ---- The publish agent ----

void requirePublishAgent(Map ctx) {
  if (!ctx.forge.publishAgent) {
    error "Merges, tags, releases and GitHub Actions run on the publish agent, and catalog.yaml has none (set forge.publishAgent, or have an environment with trigger: release)."
  }
}

// The body on the publish agent (forge.publishAgent), with this repository's tools in ./yggdrasil and,
// bound for it, the git server's write token (`write`: FORGE_TOKEN, for merges, tags, releases and
// uploads) and the GitHub App's (`github`: GH_TOKEN, for GitHub Actions and a release's GitHub copy).
// The one place a node gets either: the ci and release agents never do (scripts/test_pipeline_library.py
// checks it). The workspace and the scratch directory are wiped afterwards. Returns the body's value.
def onPublishAgent(Map ctx, boolean write, boolean github, Closure body) {
  requirePublishAgent(ctx)
  def result = null
  node(ctx.forge.publishAgent) {
    try {
      checkoutTools()
      Closure withGithub = { github ? withForgeToken('github') { body() } : body() }
      result = write ? withForgeToken('write') { withGithub() } : withGithub()
    } finally {
      cleanScratch()
      cleanWs()
    }
  }
  return result
}

// ---- The release pull request (Forgejo, and GitHub repositories with yggdrasil.yml) ----

// The release pull request, once its branch policy and checks passed: everything that can be found
// wrong is looked for first, then
//   1. the entry's actionsChecks on GitHub Actions (the commit, on the mirror for Forgejo), waited for
//      outside node() up to the first release environment's checksTimeout: a failure fails the
//      release; not finished (GitHub not answering, the mirror without the commit, still running) asks
//      the skip (askToSkip);
//   2. every trigger: release environment in catalog order, with deploy.inputs' archives as on a push,
//      deploy/<environment> after each (with "without GitHub Actions checks" after a skip);
//   3. on the publish agent: the merge (titled "release: v<version> (#N)", which release_notes.py
//      leaves out of the notes) and the tag v<version> on the merge commit;
//   4. releaseFromTag, shared with the tag builds and the release-resume job: with yggdrasil.yml's
//      release, the release files (releaseFiles: built on the release agent, the Windows ones from
//      GitHub, validated and finalized on the release agent); then, on the publish agent, the notes and
//      publish-release.sh (publishRelease), which makes the drafts, uploads and checks the files and
//      publishes the release (Forgejo, then GitHub last; after a skip Forgejo only, "GitHub copy
//      pending"). A failure here says the pull request is merged and tagged, and to finish the release
//      with the release-resume job;
//   5. the release branch deleted -- kept, with a note, where Forgejo protects it.
// The build's description is v<version> (" (deployed without GitHub Actions checks)" after a skip, with
// "; GitHub copy pending" when the release has a GitHub copy to make); it is unstable when Windows files
// are missing (published without them, listed in the notes and the log; the actions-release job
// attaches them later).
void releasePullRequest(Map ctx, Map state, List environments) {
  String app = ctx.app
  if (!environments) {
    echo "${app} has no environment with trigger: release; nothing deploys on this pull request."
    return
  }
  String version = (env.CHANGE_BRANCH =~ RELEASE_BRANCH)[0][1]
  String tag = "v${version}"
  if (!(env.CHANGE_ID ==~ '[0-9]+')) {
    error "'${printable(env.CHANGE_ID)}' is not a pull request number"
  }
  Map releaseConfig = state.config?.release as Map
  if (ctx.releaseArtifacts) {
    if (releaseConfig == null) {
      error "releaseArtifacts: true is GitHub's release-artifacts.yml path, for a repository without yggdrasil.yml on GitHub; here, declare the release files in yggdrasil.yml (release). Nothing was deployed."
    }
    echo "releaseArtifacts is left aside: yggdrasil.yml's release says what this release publishes."
  }
  if (releaseConfig != null) {
    String problem = releaseProblem(releaseConfig)
    if (problem) {
      error "yggdrasil.yml's release cannot be used: ${problem}. Nothing was deployed."
    }
    if (releaseConfig.windows && !githubActions(ctx)) {
      error "release.windows needs GitHub Actions, and this installation has no GitHub mirror (catalog.yaml mirror). Nothing was deployed."
    }
  }
  requirePublishAgent(ctx)
  int seconds = environments[0].checksTimeout as int

  boolean skipped = waitForReleaseActions(ctx, state, seconds)
  String note = skipped ? " (${SKIPPED})" : ''
  for (Map environment : environments) {
    reportStatus(ctx.repo, state.sha, 'pending', "deploy/${environment.id}", "Waiting to deploy to ${environment.name}${note}")
  }
  // The image build gets deploy.inputs' archives, as on a push (prepare ran those checks).
  List inputs = state.config != null ? deployInputs(state.config) : []
  for (Map environment : environments) {
    deployTo(app, environment, version, state.sha, false, ctx.repo, inputs, null, note)
  }

  String merged = mergeAndTag(ctx, state.sha, version, tag)
  // From here on the pull request is merged and the tag pushed: a failure cannot be undone by running
  // this build again (the merge would be refused), so it says how to finish instead.
  Map published = null
  try {
    published = releaseFromTag(ctx, state.entry, releaseConfig, tag, merged, [skipped: skipped, seconds: seconds])
  } catch (FlowInterruptedException interruption) {
    echo resumeNote(ctx, tag)
    throw interruption
  } catch (Exception problem) {
    error "${failureText(problem)}\n${resumeNote(ctx, tag)}"
  }
  String description = tag
  if (skipped) {
    description += published.githubPending ? ' (deployed without GitHub Actions checks; GitHub copy pending)'
                                           : ' (deployed without GitHub Actions checks)'
  }
  currentBuild.description = description
  echo "Released ${tag}: ${published.urls.join(' ')}"
  if (published.missing) {
    // With the GitHub copy pending, release-resume makes it and adds the Windows files; actions-release
    // would fail on the missing GitHub copy.
    String finish = published.githubPending
      ? "The release-resume job (REPOSITORY=${ctx.app}, TAG=${tag}) makes the GitHub copy and adds them."
      : 'The actions-release job attaches them.'
    unstable "${tag} was published without its Windows files: ${published.missing.join(', ')} (${published.reason}). ${finish}"
  }
  deleteReleaseBranch(ctx)
}

// What a release whose merge went through but whose tag did not tells the owner. Only forge.jenkinsUser
// may create an application's v* tags (on Forgejo not even the owner), so the owner does not tag it by
// hand: release-resume, given the merge commit, tags it (tagReleaseMerge) and finishes the release.
String untaggedNote(Map ctx, String tag, String merge) {
  return "Not released: the pull request is merged as ${merge}, but not tagged ${tag}; finish the release with the release-resume job (REPOSITORY=${ctx.app}, TAG=${tag}, COMMIT=${merge}), which tags the merge commit first."
}

// A failure's text for a message: its message, or the exception itself when it has none.
@NonCPS
String failureText(Throwable problem) {
  return problem.message ?: problem.toString()
}

// What a release that failed after its merge tells the owner.
String resumeNote(Map ctx, String tag) {
  return "Not released: the pull request is merged and tagged ${tag}; finish the release with the release-resume job (REPOSITORY=${ctx.app}, TAG=${tag})."
}

// Steps 4 and 5 of a release, from a tag that exists on `sha`: the release files (yggdrasil.yml's
// release, when declared: releaseFiles) and publishRelease, whose publish-release.sh picks up the tag, a
// draft and the files an earlier run left. The release pull request calls it after its merge; the
// owner-tagged releases (a tag build) and the release-resume job call it with the tag's commit, through
// releaseTag -- one copy of the release logic. opts:
//   skipped  the GitHub Actions checks were skipped (GitHub may not be answering): no Windows files, and
//            no GitHub copy of the release -- published on the git server only, "GitHub copy pending"
//            (the release-resume job makes the copy later, publish-release.sh being idempotent)
//   seconds  how long a Windows run may take (default 3600)
//   nuget    publish.nuget of an owner-tagged library: its packages packed and pushed (publishPackages;
//            a version the feed has already is skipped, so a rerun goes on) -- after the release files are
//            built, validated and finalized, before anything is published: a push cannot be taken back, so a release
//            whose files fail pushes nothing
//   release  false: no release on the git server (a library that only publishes packages); default true
// Returns [urls: the releases' addresses, missing: the Windows files left out, reason: why,
// githubPending: whether the GitHub copy was left for later, packages: whether packages were pushed,
// released: whether a release was published].
Map releaseFromTag(Map ctx, Map entry, Map releaseConfig, String tag, String sha, Map opts) {
  boolean skipped = opts.skipped == true
  int seconds = (opts.seconds ?: 3600) as int
  Map nuget = opts.nuget as Map
  boolean releasing = opts.release != false
  // The order: the release files built and validated (a failure stops here, nothing pushed or
  // published), then the packages (irreversible), then the release.
  Map files = releasing && releaseConfig != null ? releaseFiles(ctx, entry, releaseConfig, tag, sha, skipped, seconds)
                                                 : [stash: null, missing: [], reason: '']
  if (nuget != null) {
    publishPackages(ctx, nuget, tag, sha)
  }
  if (!releasing) {
    return [urls: [], missing: [], reason: '', githubPending: false, packages: nuget != null, released: false]
  }
  boolean githubPending = mirrorsReleases(ctx) && skipped
  if (githubPending) {
    echo "GitHub copy pending: the GitHub Actions checks were skipped, so ${tag} is published on the git server only. The release-resume job (REPOSITORY=${ctx.app}, TAG=${tag}) makes its GitHub copy."
  }
  List urls = publishRelease(ctx, entry, tagVersion(tag), tag, sha, files, mirrorsReleases(ctx) && !skipped)
  return [urls: urls, missing: files.missing, reason: files.reason, githubPending: githubPending, packages: nuget != null,
          released: true]
}

// Stage Wait for GitHub Actions: the entry's actionsChecks on the release's commit, for as long as
// `seconds`. Returns whether the release goes on without them (the skip). Exit 1 fails the release;
// exit 2 or 3 at the end (GitHub not answering, the mirror without the commit, still running) asks,
// outside node(), whether to deploy without them: the answer is recorded in the log, and the caller
// records it in the deploy/<environment> statuses and the build's description.
boolean waitForReleaseActions(Map ctx, Map state, int seconds) {
  List names = (state.entry.actionsChecks ?: []) as List
  boolean skipped = false
  stage('Wait for GitHub Actions') {
    if (!names) {
      echo 'No actionsChecks in catalog.yaml: nothing to wait for on GitHub Actions.'
      return
    }
    if (!githubActions(ctx)) {
      echo "No GitHub mirror (catalog.yaml mirror): ${names.join(', ')} cannot run, nothing to wait for."
      return
    }
    Map look = pollGithub(ctx, seconds, 'GitHub Actions', ['actions-status', ctx.repo, state.sha] + names, [0, 1],
                          ACTIONS_POLL_SECONDS)
    if (look.out?.trim()) {
      echo printable(look.out.trim())
    }
    if (look.code == 1) {
      error "GitHub Actions: a check failed (${names.join(', ')}). Not releasing."
    }
    if (look.code == 0) {
      echo "GitHub Actions: ${names.join(', ')} passed."
      return
    }
    echo "GitHub Actions checks didn't finish within ${seconds} s (actions-status exit ${look.code}): ${names.join(', ')}."
    // After the skip the Windows files are not built, and a release with release.finalize never goes out
    // without them (finalize covers the whole set): no skip to offer, the release stops here.
    if (needsWindowsFiles(state.config?.release as Map)) {
      error "Not releasing: yggdrasil.yml's release.finalize must cover the Windows files, which are not built without the GitHub Actions checks, so there is no deploying without them. Nothing was deployed; build the pull request again once GitHub Actions answers."
    }
    askToSkip()
    skipped = true
    echo "Deploying without GitHub Actions checks: ${names.join(', ')} did not finish. This is recorded in the deploy/<environment> statuses and the build's description; Windows files are not built."
  }
  return skipped
}

// The skip: outside node(), like an approval. Abort fails the release (an interruption of the build
// itself stays one).
void askToSkip() {
  try {
    input id: 'SkipActions', message: SKIP_QUESTION, ok: SKIP_BUTTON
  } catch (FlowInterruptedException interruption) {
    if (rejected(interruption)) {
      error 'Not released: the skip was declined, and GitHub Actions checks did not finish.'
    }
    throw interruption
  }
}

// Whether an input was answered with Abort (its interruption carries a Rejection).
@NonCPS
boolean rejected(FlowInterruptedException interruption) {
  return interruption.causes.any { it.getClass().simpleName == 'Rejection' }
}

// Stage Merge and tag, on the publish agent: the pull request merged at the deployed commit (merge-pr
// refuses if its head moved) with the title release_notes.py knows, and the tag on the merge commit --
// made unless an earlier run made it there already. Returns the merge commit.
String mergeAndTag(Map ctx, String sha, String version, String tag) {
  String merged = null
  stage('Merge and tag') {
    merged = onPublishAgent(ctx, true, false) {
      String commit = tool('forge.sh', ['merge-pr', ctx.repo, env.CHANGE_ID, sha, "release: v${version} (#${env.CHANGE_ID})"], true)
      if (!(commit ==~ COMMIT)) {
        error "forge.sh merge-pr answered '${printable(commit)}', not a commit"
      }
      // Merged, and not tagged yet: a failure from here says so, and how to finish.
      try {
        ensureTag(ctx, tag, commit)
      } catch (FlowInterruptedException interruption) {
        echo untaggedNote(ctx, tag, commit)
        throw interruption
      } catch (Exception problem) {
        error "${failureText(problem)}\n${untaggedNote(ctx, tag, commit)}"
      }
      return commit
    }
    echo "Merged as ${merged.take(7)}, tagged ${tag}."
  }
  return merged
}

// On the publish agent, with the write token: the tag on `sha`, created when the git server has none;
// a tag of that name on another commit fails the release.
void ensureTag(Map ctx, String tag, String sha) {
  String tmp = scratch()
  int code = toolStatus('forge.sh', ['tag-sha', ctx.repo, tag], "${tmp}/tag-sha")
  if (code == 4) {
    tool('forge.sh', ['create-tag', ctx.repo, tag, sha])
    return
  }
  if (code != 0) {
    error "forge.sh tag-sha exited ${code}; its message is above."
  }
  String tagged = readScratch(tmp, 'tag-sha').trim()
  if (tagged != sha) {
    error "The tag ${tag} is on ${printable(tagged).take(7)}, not on the release's ${sha.take(7)}."
  }
  echo "The tag ${tag} is on ${sha.take(7)} already."
}

// Stage Clean up, on the publish agent: the release branch deleted. Forgejo protects release/* (exit 3:
// the server keeps it), which is a note, not a failure.
void deleteReleaseBranch(Map ctx) {
  stage('Clean up') {
    onPublishAgent(ctx, true, false) {
      int code = toolStatus('forge.sh', ['delete-branch', ctx.repo, env.CHANGE_BRANCH], "${scratch()}/delete-branch")
      if (code == 3) {
        echo "${printable(env.CHANGE_BRANCH)} ${KEPT_BRANCH}"
      } else if (code != 0) {
        error "forge.sh delete-branch exited ${code}; its message is above."
      }
    }
  }
}

// ---- Release files (shared with the owner-tagged releases and the actions-release job) ----

// Stage Release files, for yggdrasil.yml's release on the tag `tag` (commit `sha`):
//   - release.build on the release agent: the tag checked out with the read credential, the build run
//     with run-check.sh in its image (credentials from release.build.credentials, allow-listed against
//     the entry's releaseCredentials), each of release.build.files stashed (buildReleaseFiles);
//   - release.windows: the workflow dispatched on the tag, its files from GitHub (windowsFiles); after
//     a skip, or when that fails, they are missing;
//   - everything gathered on the release agent, release.validate run over it, and the validated set
//     stashed as release-files (gatherReleaseFiles); then release.finalize, whose files join the set
//     (finalizeReleaseFiles, in a node of its own: the stash release-upload).
// Returns [stash: the upload set's stash, replace: whether a draft's files are uploaded again (with
// release.finalize), missing: the missing Windows files (globs, or the workflow's
// name), reason: why they are missing].
Map releaseFiles(Map ctx, Map entry, Map releaseConfig, String tag, String sha, boolean skipped, int seconds) {
  Map files = [stash: null, missing: [], reason: '']
  stage('Release files') {
    List built = releaseConfig.build ? buildReleaseFiles(ctx, entry, releaseConfig.build as Map, tag, sha) : []
    List windows = []
    if (releaseConfig.windows) {
      Map got = skipped ? [stashes: null, reason: 'GitHub Actions checks were skipped'] : windowsFiles(ctx, releaseConfig.windows as Map, tag, seconds)
      if (got.stashes == null) {
        files.missing = windowsNames(releaseConfig.windows as Map)
        files.reason = got.reason
        // release.finalize covers the whole set: never a partial one. The release stops before anything is
        // published, as a failed validation does; release-resume runs windows, validate and finalize again.
        if (needsWindowsFiles(releaseConfig)) {
          error "The Windows files are missing (${got.reason}): ${files.missing.join(', ')}. release.finalize must cover the whole set, so the release does not go on without them: nothing is published, and no draft release was made."
        }
        echo "The Windows files are missing (${got.reason}): ${files.missing.join(', ')}. The release goes on without them."
      } else {
        windows = got.stashes
      }
    }
    files.stash = gatherReleaseFiles(ctx, entry, releaseConfig, tag, sha, built, windows)
    // A manifest signed now describes this run's files: a draft's older ones are uploaded again.
    files.replace = releaseConfig.finalize != null
  }
  return files
}

// On the release agent: the tag checked out, release.build run in its image (from the application's
// root, with RELEASE_TAG and RELEASE_VERSION and its allow-listed credentials in its env file), then
// each glob of release.build.files stashed (release-build-<n>) after LINK_GUARD -- a glob matching no
// file fails the release, a link leading out of the checkout refuses all of them. Returns the stashes.
List buildReleaseFiles(Map ctx, Map entry, Map build, String tag, String sha) {
  List globs = (build.files ?: []) as List
  List stashes = []
  node('release') {
    try {
      checkoutTag(ctx, tag, sha)
      withCheckCredentials([name: 'release-build', credentials: build.credentials], entry, 'releaseCredentials') { String envFile ->
        withEnv(["YGG_IMAGE=${build.image}", "YGG_RUN=${build.run}", "YGG_ENV_FILE=${envFile ?: "${scratch()}/release-build.env"}",
                 "YGG_TAG=${tag}", "YGG_VERSION=${tagVersion(tag)}"]) {
          int code = sh(script: '''
            umask 077
            mkdir -p "$(dirname "$YGG_ENV_FILE")"
            printf 'RELEASE_TAG=%s\\nRELEASE_VERSION=%s\\n' "$YGG_TAG" "$YGG_VERSION" >>"$YGG_ENV_FILE"
            bash yggdrasil/scripts/run-check.sh release-build "$YGG_IMAGE" "$WORKSPACE/app" --env-file "$YGG_ENV_FILE" \\
              -- sh -c "$YGG_RUN"
          ''', returnStatus: true)
          if (code != 0) {
            error "release.build failed (exit ${code}): nothing is published."
          }
        }
      }
      dir('app') {
        refuseLinks(false, 'the release build')
        for (int i = 0; i < globs.size(); i++) {
          String name = "release-build-${i}"
          stashGlob(name, globs[i] as String, 'release.build.files')
          stashes << name
        }
      }
    } finally {
      cleanScratch()
      cleanWs()
    }
  }
  return stashes
}

// The Windows files of a release from GitHub: once the mirror has pushed the tag (on GitHub itself it is
// there), the repository's release.windows.workflow is dispatched on it with tag=<tag>, its run watched
// until it ends or `seconds` run out, and its artifacts downloaded. Every step is a short call on the
// publish agent -- the GitHub App's token never reaches the release agent -- and every wait between
// them is outside node(). The files matching release.windows.files (every file without it, the
// artifacts each in a directory of its name) are stashed there as windows-files-<n>. Returns
// [stashes] when they are there, [stashes: null, reason] when they could not be had.
Map windowsFiles(Map ctx, Map windows, String tag, int seconds) {
  String workflow = windows.workflow
  Map look = pollGithub(ctx, TAG_WAIT_SECONDS, "${tag} on GitHub", ['wait-ref', ctx.repo, "refs/tags/${tag}", '0'], [0],
                        TAG_POLL_SECONDS)
  if (!look.finished) {
    return [stashes: null, reason: "${tag} did not reach GitHub within ${TAG_WAIT_SECONDS} s"]
  }
  look = githubLook(ctx, ['dispatch', ctx.repo, workflow, tag, "tag=${tag}"])
  String run = (look.out ?: '').trim()
  if (look.code != 0 || !(run ==~ '[0-9]{1,20}')) {
    return [stashes: null, reason: "${workflow} could not be started on ${tag} (dispatch exit ${look.code})"]
  }
  echo "GitHub Actions: ${workflow} runs on ${tag} as run ${run}."
  look = pollGithub(ctx, seconds, "${workflow} run ${run}", ['run-status', ctx.repo, run], [0, 1], ACTIONS_POLL_SECONDS)
  if (look.code != 0) {
    return [stashes: null, reason: look.finished ? "${workflow} run ${run} failed" : "${workflow} run ${run} did not finish within ${seconds} s"]
  }
  List globs = windows.files ? (windows.files as List) : ['**']
  try {
    return onPublishAgent(ctx, false, true) {
      String downloads = "${env.WORKSPACE}/windows-download"
      int code = 2
      withEnv(["GITHUB_OWNER=${githubOwner(ctx)}"]) {
        code = toolStatus('github.sh', ['download-artifacts', ctx.repo, run, downloads], '')
      }
      if (code != 0) {
        return [stashes: null, reason: "the artifacts of ${workflow} run ${run} could not be downloaded (exit ${code})"]
      }
      List stashes = []
      dir('windows-download') {
        refuseLinks(true, 'the Windows files')
        for (int i = 0; i < globs.size(); i++) {
          String name = "windows-files-${i}"
          stashGlob(name, globs[i] as String, 'release.windows.files')
          stashes << name
        }
      }
      return [stashes: stashes]
    }
  } catch (FlowInterruptedException interruption) {
    throw interruption
  } catch (Exception problem) {
    return [stashes: null, reason: "the files of ${workflow} run ${run} could not be had: ${safeText(failureText(problem), 300)}"]
  }
}

// Whether a release cannot go out without its Windows files: it declares release.windows and
// release.finalize, which must cover the whole set. Such a release is never skipped past its GitHub Actions
// checks (waitForReleaseActions), stops when the Windows files are missing (releaseFiles), and gets no
// Windows files attached afterwards (actionsRelease): release-resume makes it whole.
@NonCPS
boolean needsWindowsFiles(Map releaseConfig) {
  return releaseConfig != null && releaseConfig.windows != null && releaseConfig.finalize != null
}

// What a release lists as missing when its Windows files could not be had: release.windows.files, or
// the workflow's name when it declares none.
@NonCPS
List windowsNames(Map windows) {
  return windows.files ? (windows.files as List).collect { it.toString() } : [windows.workflow.toString()]
}

// On the release agent: the release's files in release-files/ (release.build's under build/, at their
// paths in the checkout; the Windows ones under windows/<artifact>/), with no link among them, stashed as
// release-files -- the set publish-release.sh uploads and checks, exactly the declared globs' matches --
// before release.validate runs. The validation is pass or fail only: it runs over a copy (release-check/,
// RELEASE_FILES in its environment), in its image, from the application's root (the tag checked out), the
// whole workspace mounted, with RELEASE_TAG and RELEASE_VERSION; it runs as root and can write anywhere
// there, but the stash is taken, so nothing it adds or changes reaches the release. A validation that fails
// stops the release here: publish-release.sh, which is what creates the drafts, is never called, so there
// is nothing to publish (and no draft at all). Then release.finalize, when declared, in a node of its own
// (finalizeReleaseFiles), and the upload set is release-files plus what it adds. Returns the upload set's
// stash.
String gatherReleaseFiles(Map ctx, Map entry, Map releaseConfig, String tag, String sha, List built, List windows) {
  Map validate = releaseConfig.validate as Map
  Map finalize = releaseConfig.finalize as Map
  node('release') {
    try {
      if (validate) {
        checkoutTag(ctx, tag, sha)
      }
      for (String directory : ['release-files', 'release-check']) {
        dir(directory) {
          deleteDir()
        }
      }
      sh(script: 'mkdir -p release-files/build release-files/windows')
      dir('release-files/build') {
        for (String name : built) {
          unstash name
        }
      }
      dir('release-files/windows') {
        for (String name : windows) {
          unstash name
        }
      }
      dir('release-files') {
        refuseLinks(true, 'the release files')
        refuseDuplicateNames('the release files')
        // The upload set, before anything else runs here.
        stash name: 'release-files', includes: '**', allowEmpty: true
      }
      if (validate) {
        withEnv(["YGG_IMAGE=${validate.image}", "YGG_RUN=${validate.run}", "YGG_TMP=${scratch()}", "YGG_TAG=${tag}",
                 "YGG_VERSION=${tagVersion(tag)}"]) {
          int code = sh(script: '''
            umask 077
            mkdir -p "$YGG_TMP"
            cp -R release-files release-check
            env_file="$YGG_TMP/release-validate.env"
            {
              printf 'RELEASE_FILES=%s\\n' "$WORKSPACE/release-check"
              printf 'RELEASE_TAG=%s\\n' "$YGG_TAG"
              printf 'RELEASE_VERSION=%s\\n' "$YGG_VERSION"
            } >"$env_file"
            bash yggdrasil/scripts/run-check.sh release-validate "$YGG_IMAGE" "$WORKSPACE" --env-file "$env_file" \\
              -- sh -c 'cd app && exec sh -c "$1"' sh "$YGG_RUN"
          ''', returnStatus: true)
          if (code != 0) {
            error "release.validate failed (exit ${code}): nothing is published, and no draft release was made."
          }
        }
      }
      // Nothing runs here after the validation: it had the whole workspace, yggdrasil/scripts/ included, as
      // root. finalize, which runs a script of the workspace on the host with its credentials bound, gets a
      // node and a checkout of its own.
    } finally {
      cleanScratch()
      cleanWs()
    }
  }
  return finalize ? finalizeReleaseFiles(ctx, entry, finalize, tag, sha) : 'release-files'
}

// On the release agent, in a node of its own, after gatherReleaseFiles (release-files stashed, and
// validated): release.finalize, the step that makes what must cover the whole set -- a manifest of every
// file, its signature. Its workspace starts from a fresh checkout of the tag and of yggdrasil's tools
// (checkoutTag, as release.build's does): release.validate ran as root with its whole workspace mounted,
// yggdrasil/scripts/ included, so nothing of that workspace -- a rewritten run-check.sh, a file its globs
// would match -- may reach this step, which runs run-check.sh on the host with its credentials bound. It
// sees the set as release.validate does: RELEASE_FILES is release-check/ (unstashed from release-files),
// and it runs in its image, from the application's root, the whole workspace mounted, with
// RELEASE_TAG, RELEASE_VERSION and the credentials release.finalize.credentials names (allow-listed
// against the entry's releaseCredentials, as release.build's are). It adds files and changes none: the set
// is checksummed before and after (RELEASE_SUMS: every path with its type, every file's SHA-256), and a
// path changed, added or removed there fails the release, naming it. Its own files are its `files` globs,
// matched in the checkout like release.build's (a glob matching no file fails, a link leading out of the
// checkout refuses them all), and go into the upload set under finalize/. Any failure stops the release
// before anything is published, as a failed validation does. Returns the upload set's stash,
// release-upload: release-files and finalize/.
String finalizeReleaseFiles(Map ctx, Map entry, Map finalize, String tag, String sha) {
  String upload = null
  node('release') {
    try {
      checkoutTag(ctx, tag, sha)
      upload = finalizeInWorkspace(entry, finalize, tag)
    } finally {
      cleanScratch()
      cleanWs()
    }
  }
  return upload
}

// finalizeReleaseFiles' steps, in its node: the tag and the tools checked out there, nothing else yet.
String finalizeInWorkspace(Map entry, Map finalize, String tag) {
  List globs = (finalize.files ?: []) as List
  for (String directory : ['release-check', 'release-upload']) {
    dir(directory) {
      deleteDir()
    }
  }
  dir('release-check') {
    unstash 'release-files'
  }
  String before = releaseSums()
  withCheckCredentials([name: 'release-finalize', credentials: finalize.credentials], entry, 'releaseCredentials') { String envFile ->
    withEnv(["YGG_IMAGE=${finalize.image}", "YGG_RUN=${finalize.run}", "YGG_ENV_FILE=${envFile ?: "${scratch()}/release-finalize.env"}",
             "YGG_TAG=${tag}", "YGG_VERSION=${tagVersion(tag)}"]) {
      int code = sh(script: '''
        umask 077
        mkdir -p "$(dirname "$YGG_ENV_FILE")"
        {
          printf 'RELEASE_FILES=%s\\n' "$WORKSPACE/release-check"
          printf 'RELEASE_TAG=%s\\n' "$YGG_TAG"
          printf 'RELEASE_VERSION=%s\\n' "$YGG_VERSION"
        } >>"$YGG_ENV_FILE"
        bash yggdrasil/scripts/run-check.sh release-finalize "$YGG_IMAGE" "$WORKSPACE" --env-file "$YGG_ENV_FILE" \\
          -- sh -c 'cd app && exec sh -c "$1"' sh "$YGG_RUN"
      ''', returnStatus: true)
      if (code != 0) {
        error "release.finalize failed (exit ${code}): nothing is published, and no draft release was made."
      }
    }
  }
  // The container had the whole workspace as root. What the host reads from it from here on must still be
  // the workspace's own: app/ (where finalize's files are matched) and release-check/ (what is checksummed)
  // real directories, not links to elsewhere on the agent; release-upload/ is made anew below, whatever the
  // step put there.
  int layout = sh(script: '''
    for directory in app release-check; do
      if [ -L "$directory" ] || [ ! -d "$directory" ]; then
        echo "$directory is no longer a directory of the workspace" >&2
        exit 3
      fi
    done
  ''', returnStatus: true)
  if (layout != 0) {
    error "release.finalize replaced app/ or release-check/ with something else (exit ${layout}): refused. Nothing is published, and no draft release was made."
  }
  String after = releaseSums()
  if (after != before) {
    error "release.finalize changed the release files, which it may only add to: ${sumsChanged(before, after).join(', ')}. Nothing is published, and no draft release was made."
  }
  List stashes = []
  dir('app') {
    refuseLinks(false, 'the release finalize step')
    for (int i = 0; i < globs.size(); i++) {
      String name = "release-finalize-${i}"
      stashGlob(name, globs[i] as String, 'release.finalize.files')
      stashes << name
    }
  }
  // The upload set is built only now, after the container: anything the step wrote into release-upload/ (it
  // could, with the whole workspace mounted) is deleted with it, and rm does not follow a link put there.
  sh(script: 'rm -rf -- release-upload && mkdir -p release-upload/finalize')
  dir('release-upload') {
    unstash 'release-files'
  }
  dir('release-upload/finalize') {
    for (String name : stashes) {
      unstash name
    }
  }
  dir('release-upload') {
    refuseLinks(true, 'the release files')
    refuseDuplicateNames("the release files and release.finalize's")
    stash name: 'release-upload', includes: '**', allowEmpty: true
  }
  return 'release-upload'
}

// RELEASE_SUMS run in release-check/: the list finalizeReleaseFiles compares before and after the step.
// A checksum that cannot be taken (a file the step made unreadable) fails the release too.
String releaseSums() {
  String sums = ''
  dir('release-check') {
    sums = sh(script: RELEASE_SUMS, returnStdout: true)
  }
  return sums
}

// The paths whose line in RELEASE_SUMS differs between `before` and `after` (changed, removed or added),
// each once and at most 20: what the failure names.
@NonCPS
List sumsChanged(String before, String after) {
  Set was = new LinkedHashSet(before.readLines())
  Set now = new LinkedHashSet(after.readLines())
  Set paths = new LinkedHashSet()
  for (String line : ((was - now) + (now - was))) {
    // "<type> <path>" or "<sha256>  <path>".
    paths << printable(line.replaceFirst('^(?:[a-z] |[0-9a-f]{64}  )', ''))
  }
  List named = new ArrayList(paths)
  return named ? named.take(20) : ['the checksums differ']
}

// Stage Publish the release, on the publish agent: the notes (release_notes.py: the version's CHANGELOG.md
// section, else the pull requests merged since the previous tag), the files (the upload set's stash,
// files.stash -- release-files, or release-upload with release.finalize's files -- and nothing else) and
// publish-release.sh, with the release's GitHub copy when `mirrorReleases` (the catalog's
// mirror.releases, unless the GitHub Actions checks were skipped: releaseFromTag) -- the tag waited for
// first, outside node(): the mirror pushes it. With release.finalize, publish-release.sh gets
// --replace-files: a draft's files of the same name are uploaded again, not reused by their size, so a
// resumed release never keeps an old file its new manifest does not describe. Returns the releases'
// addresses.
List publishRelease(Map ctx, Map entry, String version, String tag, String sha, Map files, boolean mirrorReleases) {
  List urls = []
  stage('Publish the release') {
    if (mirrorReleases) {
      Map look = pollGithub(ctx, TAG_WAIT_SECONDS, "${tag} on GitHub", ['wait-ref', ctx.repo, "refs/tags/${tag}", '0'], [0],
                            TAG_POLL_SECONDS)
      if (!look.finished) {
        error "${tag} did not reach GitHub within ${TAG_WAIT_SECONDS} s: its GitHub copy cannot be made. Nothing is published; start the build again once the mirror has pushed it."
      }
    }
    urls = onPublishAgent(ctx, true, mirrorReleases) {
      // The history and the tags, for the notes: the read credential, as on every other agent.
      checkoutCommit(ctx, sha)
      String tmp = scratch()
      dir('release-files') {
        deleteDir()
      }
      sh(script: 'mkdir -p release-files')
      dir('release-files') {
        if (files.stash) {
          unstash files.stash
        }
        refuseLinks(true, 'the release files')
      }
      // The previous tag the notes start from: one of the same package for a <package>@<version> tag.
      withEnv(["YGG_TMP=${tmp}", "YGG_TAG=${tag}", "YGG_VERSION=${version}",
               "YGG_TAGS=${previousTagPattern(tag, (entry?.tags ?: ctx.tags) as String)}"]) {
        sh(script: '''
          mkdir -p "$YGG_TMP"
          set --
          if previous=$(git -C app describe --tags --abbrev=0 --match "$YGG_TAGS" --exclude "$YGG_TAG" HEAD 2>/dev/null); then
            set -- "$previous"
          fi
          python3 yggdrasil/scripts/release_notes.py app "$YGG_VERSION" "$@" >"$YGG_TMP/notes.md"
        ''')
      }
      List arguments = [ctx.repo, tag, sha, tag, "${tmp}/notes.md", "${env.WORKSPACE}/release-files"]
      if (mirrorReleases) {
        arguments << '--mirror-releases'
      }
      if (files.replace) {
        arguments << '--replace-files'
      }
      if (files.missing) {
        arguments += ['--missing', files.missing.join(',')]
      }
      String out = ''
      // The tag is on GitHub already (above): publish-release.sh's own wait for it is a last look.
      withEnv(["GITHUB_OWNER=${githubOwner(ctx)}", 'GITHUB_TAG_WAIT=30']) {
        out = tool('publish-release.sh', arguments, true)
      }
      return (out ?: '').readLines().findAll { it.trim() }
    }
  }
  return urls
}

// Whether a release gets a GitHub copy: Forgejo with a mirror whose releases are on (the default).
boolean mirrorsReleases(Map ctx) {
  return ctx.forge.type == 'forgejo' && ctx.mirror != null && ctx.mirror.releases
}

// On the release (or publish) agent: the commit `sha` in ./app (checkoutCommit, the read credential),
// which the tag `tag` must point to.
String checkoutTag(Map ctx, String tag, String sha) {
  if (!(tag ==~ TAG)) {
    error "'${printable(tag)}' is not a tag"
  }
  String head = checkoutCommit(ctx, sha)
  withEnv(["YGG_TAG=${tag}"]) {
    String tagged = sh(script: 'git -C app rev-parse --verify --quiet "refs/tags/$YGG_TAG^{commit}" || true', returnStdout: true).trim()
    if (tagged != sha) {
      error "The tag ${tag} is on '${printable(tagged).take(7)}', not on ${sha.take(7)}."
    }
  }
  return head
}

// In the current directory: LINK_GUARD -- with `any`, no symbolic link at all, else none leading out of it.
void refuseLinks(boolean any, String what) {
  withEnv(["YGG_ANY_LINK=${any ? '1' : ''}"]) {
    int code = sh(script: LINK_GUARD, returnStatus: true)
    if (code != 0) {
      error "${what}: refused, a symbolic link is among them (exit ${code}); nothing of them is used."
    }
  }
}

// In the current directory, the upload set (release-files/, or release-upload/ with release.finalize's
// files): DUPLICATE_NAMES must find nothing. On the release agent, before anything irreversible -- a library's
// NuGet push, a draft -- so two files of one name fail the release here, naming them, instead of in
// publish-release.sh after the push.
void refuseDuplicateNames(String what) {
  String names = sh(script: DUPLICATE_NAMES, returnStdout: true)
  if (names) {
    error "${what}: ${duplicateText(names)}; a release has one file per name (each is uploaded by its name). Nothing is published, and no draft release was made."
  }
}

// DUPLICATE_NAMES' answer as the failure says it.
@NonCPS
String duplicateText(String names) {
  List named = names.split('\u0000').findAll { it }.collect { printable(it) }
  String more = named.size() > 20 ? " and ${named.size() - 20} more" : ''
  return "more than one file is named ${named.take(20).join(', ')}${more}"
}

// One glob's files from the current directory, as the stash `name`: none fails the release (a declared
// file that is not there is a broken release).
void stashGlob(String name, String glob, String field) {
  try {
    stash name: name, includes: glob, allowEmpty: false
  } catch (FlowInterruptedException interruption) {
    throw interruption
  } catch (Exception problem) {
    error "${field}: '${printable(glob)}' matches no file (${safeText(failureText(problem), 200)})"
  }
}

// What a release section must look like before anything of it is used (pipeline_config.py checks the
// fields; this is the library's own look at what reaches a shell, a stash or GitHub). null when fine.
@NonCPS
String releaseProblem(Map releaseConfig) {
  for (String part : ['build', 'validate', 'finalize']) {
    Map section = releaseConfig[part] as Map
    if (section == null) {
      continue
    }
    if (!(section.image instanceof String) || !(section.image ==~ IMAGE)) {
      return "release.${part}.image: '${printable(section.image)}' is not an image"
    }
    if (!(section.run instanceof String) || !section.run.trim()) {
      return "release.${part}.run: no command to run"
    }
  }
  Map build = releaseConfig.build as Map
  Map windows = releaseConfig.windows as Map
  Map finalize = releaseConfig.finalize as Map
  if (windows != null && !(windows.workflow instanceof String && windows.workflow ==~ WORKFLOW)) {
    return "release.windows.workflow: '${printable(windows?.workflow)}' is not a workflow file (name.yml)"
  }
  for (Map files : [[field: 'release.build.files', globs: build?.files], [field: 'release.windows.files', globs: windows?.files],
                    [field: 'release.finalize.files', globs: finalize?.files]]) {
    for (Object glob : (files.globs ?: []) as List) {
      if (!usableGlob(glob)) {
        return "${files.field}: '${printable(glob)}' is not a relative glob (letters, digits, . _ * ? + @ = - and /, no . or .. part)"
      }
    }
  }
  return null
}

// What publish.nuget must look like before anything of it is used (as releaseProblem). null when fine.
@NonCPS
String nugetProblem(Map nuget) {
  if (!(nuget.image instanceof String) || !(nuget.image ==~ IMAGE)) {
    return "publish.nuget.image: '${printable(nuget.image)}' is not an image"
  }
  if (!(nuget.run instanceof String) || !nuget.run.trim()) {
    return 'publish.nuget.run: no command to run'
  }
  if (nuget.packages != null && !usableGlob(nuget.packages)) {
    return "publish.nuget.packages: '${printable(nuget.packages)}' is not a relative glob (letters, digits, . _ * ? + @ = - and /, no . or .. part)"
  }
  return null
}

// A glob the stash step takes as it is meant (RELEASE_GLOB), with no ., .. or empty part.
@NonCPS
boolean usableGlob(Object glob) {
  return glob instanceof String && glob ==~ RELEASE_GLOB && !glob.split('/').any { it in ['.', '..', ''] }
}

// ---- Owner-tagged releases (a tag build) and the release-resume job ----

// A tag build. Only for a repositories entry with release: owner-tag, and a tag matching its `tags` (the
// Job DSL discovers tags for those entries alone, and only young ones: casc.yaml); any other tag is
// "Nothing to do". The owner pushed it (Forgejo's tag rules let nobody else make such a tag), so under the
// repository's release lock: the tag read on the release agent, its commit checked to be on main before
// anything else (readReleaseTag), then what its yggdrasil.yml declares (releaseTag). Without a
// yggdrasil.yml at the tag there is nothing to do either: the repository behaves as before the file
// existed.
void tagBuild(Map ctx) {
  String tag = env.TAG_NAME
  if (ctx.release != 'owner-tag') {
    echo "Nothing to do for the tag ${printable(tag)}: ${ctx.app} is not released by its owner's tags (catalog.yaml: release ${ctx.release})."
    return
  }
  if (!isVersionTag(ctx.tags, tag)) {
    echo "Nothing to do for the tag ${printable(tag)}: not a version tag of ${ctx.app} (${ctx.tags})."
    return
  }
  lock(resource: "release-${ctx.app}") {
    Map described = readReleaseTag(ctx, tag)
    if (described.config == null) {
      echo "Nothing to do for the tag ${tag}: ${ctx.app} has no yggdrasil.yml at it, so it declares no release and no packages."
      return
    }
    reportTagRelease(tag, releaseTag(ctx, tag, described), tag)
  }
}

// The release-resume job (platform/jenkins/controller/casc.yaml, its script jenkins/release-resume.groovy
// kept in Jenkins): what a tag releases, for a tag that exists already. REPOSITORY is a catalog id that
// releases (an application with a trigger: release environment, or release: owner-tag), TAG one of its
// version tags; the tag must exist and be on main (readReleaseTag). Then releaseTag, with the GitHub copy
// as the catalog says (mirror.releases): it finishes a release that failed after its merge, makes the
// GitHub copy of one published after the skip (and adds the Windows files it was published without), or
// pushes the packages a tag build did not get to. Everything is idempotent (publish-release.sh reuses the
// tag, the releases and the files there; nuget-push.sh skips the versions the feed has), so running it
// twice is harmless. Under the repository's release lock, like a release pull request.
//
// COMMIT, optional: an application's release whose pull request was merged but never tagged (the build
// said so, naming the merge commit). The commit is tagged TAG first, when it is that release's merge on
// main (tagReleaseMerge), then the job goes on as above. Not for an owner-tagged repository: its tags are
// the owner's to make.
void releaseResume() {
  String app = (params.REPOSITORY ?: '').toString().trim()
  String tag = (params.TAG ?: '').toString().trim()
  // A job made before the parameter existed has no COMMIT at all.
  String commit = (params.COMMIT ?: '').toString().trim()
  Map ctx = catalogContext(app)
  if (!(ctx.release in ['deploy', 'owner-tag'])) {
    error "${app} does not release (catalog.yaml: an application with no trigger: release environment, or a repositories entry without release: owner-tag)."
  }
  if (!isVersionTag(ctx.tags, tag)) {
    error "'${printable(tag)}' is not a version tag of ${app} (${ctx.tags})"
  }
  if (commit && !(commit ==~ COMMIT)) {
    error "COMMIT '${printable(commit)}' is not a commit (the full 40-character hash the build log named)."
  }
  if (commit && ctx.release != 'deploy') {
    error "COMMIT is only for an application's release whose merge Jenkins did not tag; ${app}'s tags are its owner's to make."
  }
  timeout(time: 4, unit: 'HOURS') {
    withForgeEnvironment(ctx) {
      lock(resource: "release-${app}") {
        if (commit) {
          tagReleaseMerge(ctx, tag, commit)
        }
        Map described = readReleaseTag(ctx, tag)
        // An application releases without a yggdrasil.yml too (a release with its notes only, as its
        // release pull request did on Forgejo); an owner-tagged repository does not, and on GitHub such an
        // application's releases are github.sh's (release()), not this path's.
        if (described.config == null && (ctx.release == 'owner-tag' || ctx.forge.type == 'github')) {
          error "${app} has no yggdrasil.yml at ${tag}: there is nothing this job releases."
        }
        reportTagRelease(tag, releaseTag(ctx, tag, described), "${app} ${tag}")
      }
    }
  }
}

// Stage Tag the merge, for release-resume's COMMIT. On the release agent with the read token (nothing of
// the repository runs): the commit checked out with every branch, and it must be on main's first-parent
// line, a merge commit (two parents) and titled as mergeAndTag titles a release's merge, "release: <tag>
// (#N)". Only forge.jenkinsUser merges into an application's main, and only mergeAndTag writes that title
// on main's own line, so such a commit is the merge of that release -- not any commit of main the owner
// names. Being an ancestor of main is not enough: a branch can carry a hand-made two-parent commit with that
// title, which reaches main as a commit merged in from develop, off the first-parent line. A tag already there is
// left as it is when it is on the commit, and fails the job on any other. Then the tag is made on the
// publish agent, by ensureTag, the release pull request's own step.
void tagReleaseMerge(Map ctx, String tag, String commit) {
  stage('Tag the merge') {
    boolean tagged = false
    node('release') {
      try {
        checkoutCommit(ctx, commit)
        String tmp = scratch()
        int code = withForgeToken('read') {
          return toolStatus('forge.sh', ['tag-sha', ctx.repo, tag], "${tmp}/tag-sha")
        }
        if (code == 0) {
          String sha = readScratch(tmp, 'tag-sha').trim()
          if (sha != commit) {
            error "Not released: ${tag} exists already, on ${printable(sha).take(7)}, not on ${commit.take(7)}."
          }
          tagged = true
        } else if (code != 4) {
          error "forge.sh tag-sha exited ${code}; its message is above."
        }
        withEnv(["YGG_SHA=${commit}"]) {
          // grep finds nothing when rev-list fails too, so an unreadable main refuses the commit as well.
          int onMain = sh(script: 'git -C app rev-list --first-parent refs/remotes/origin/main | grep -qx "$YGG_SHA"', returnStatus: true)
          if (onMain != 0) {
            error "Not released: ${commit.take(7)} is not one of main's own merges (on main's first-parent line)."
          }
          List parents = sh(script: 'git -C app rev-list --parents -n 1 "$YGG_SHA"', returnStdout: true).trim().split(/\s+/) as List
          String subject = sh(script: 'git -C app log -1 --format=%s "$YGG_SHA"', returnStdout: true).trim()
          if (parents.size() != 3 || !(subject ==~ ('release: ' + java.util.regex.Pattern.quote(tag) + ' \\(#[0-9]+\\)'))) {
            error "Not released: ${commit.take(7)} is not the merge of the release ${tag} (a merge commit titled 'release: ${tag} (#N)'; it is '${printable(subject)}')."
          }
        }
        echo "${commit.take(7)} is the merge of the release ${tag}, on main."
      } finally {
        cleanScratch()
        cleanWs()
      }
    }
    if (!tagged) {
      onPublishAgent(ctx, true, false) {
        ensureTag(ctx, tag, commit)
      }
    }
  }
}

// Stage Read the tag, on the release agent with the read token: the tag's commit (forge.sh tag-sha, an
// annotated tag peeled), checked out with the history of every branch (checkoutTag: the tag must point to
// it), which must be on main -- an owner's tag elsewhere releases nothing ("the tag is not on main"); then
// yggdrasil.yml at the tag (describeCheckout). Nothing of the repository runs here. Returns [sha, entry,
// config (null without yggdrasil.yml)]. Fails when the tag does not exist.
Map readReleaseTag(Map ctx, String tag) {
  Map described = null
  stage('Read the tag') {
    node('release') {
      try {
        checkoutTools()
        String tmp = scratch()
        int code = withForgeToken('read') {
          return toolStatus('forge.sh', ['tag-sha', ctx.repo, tag], "${tmp}/tag-sha")
        }
        if (code == 4) {
          error "${ctx.app} has no tag ${tag}."
        }
        if (code != 0) {
          error "forge.sh tag-sha exited ${code}; its message is above."
        }
        String sha = readScratch(tmp, 'tag-sha').trim()
        if (!(sha ==~ COMMIT)) {
          error "forge.sh tag-sha answered '${printable(sha)}', not a commit"
        }
        checkoutTag(ctx, tag, sha)
        withEnv(["YGG_SHA=${sha}"]) {
          int onMain = sh(script: 'git -C app merge-base --is-ancestor "$YGG_SHA" refs/remotes/origin/main', returnStatus: true)
          if (onMain == 1) {
            error "Not released: the tag is not on main (${tag} is on ${sha.take(7)}, which main does not contain)."
          }
          if (onMain != 0) {
            error "Not released: git merge-base exited ${onMain}, so whether ${tag} is on main is not known."
          }
        }
        echo "${tag} is on ${sha.take(7)}, on main."
        Map document = describeCheckout(ctx)
        described = [sha: sha, entry: document.entry, config: document.config]
      } finally {
        cleanScratch()
        cleanWs()
      }
    }
  }
  return described
}

// What a tag releases, from its yggdrasil.yml (`described`: readReleaseTag's answer), through
// releaseFromTag -- the release pull request's own steps after its merge: the files of `release`, when
// declared, built and validated; an owner-tagged library's publish.nuget packed and pushed; then the
// release (the files and the notes) unless the library declares publish.nuget and no release: a library's
// packages are its release. Everything is checked before any of it runs. Returns releaseFromTag's
// answer.
Map releaseTag(Map ctx, String tag, Map described) {
  Map config = (described.config ?: [:]) as Map
  Map releaseConfig = config.release as Map
  Map nuget = ctx.release == 'owner-tag' ? (((config.publish ?: [:]) as Map).nuget as Map) : null
  boolean releasing = releaseConfig != null || nuget == null
  if (releaseConfig != null) {
    String problem = releaseProblem(releaseConfig)
    if (problem) {
      error "yggdrasil.yml's release cannot be used: ${problem}. Nothing is released."
    }
    if (releaseConfig.windows && !githubActions(ctx)) {
      error 'release.windows needs GitHub Actions, and this installation has no GitHub mirror (catalog.yaml mirror). Nothing is released.'
    }
  }
  if (nuget != null) {
    String problem = nugetProblem(nuget)
    if (problem) {
      error "yggdrasil.yml's publish.nuget cannot be used: ${problem}. Nothing is published."
    }
  }
  if (releasing) {
    requirePublishAgent(ctx)
  }
  int seconds = ((ctx.plan.find { it.trigger == 'release' }?.checksTimeout) ?: ctx.checksTimeout) as int
  return releaseFromTag(ctx, described.entry, releaseConfig, tag, described.sha,
                        [nuget: nuget, release: releasing, seconds: seconds])
}

// The build's description (`what`: what was released, then what was done) and the log; unstable when
// Windows files are missing (the actions-release job attaches them).
void reportTagRelease(String tag, Map published, String what) {
  List done = []
  if (published.packages) {
    done << 'packages pushed'
  }
  if (published.released) {
    done << 'released'
    echo "Released ${tag}: ${published.urls.join(' ')}"
  }
  currentBuild.description = "${what}: ${done.join(', ')}"
  if (published.missing) {
    unstable "${tag} was published without its Windows files: ${published.missing.join(', ')} (${published.reason}). The actions-release job attaches them."
  }
}

// Stage Packages, publish.nuget of an owner-tagged library at the tag: packed (packPackages), then pushed
// (pushPackages) -- two steps on the release agent, so the key is never where the repository's code runs.
void publishPackages(Map ctx, Map nuget, String tag, String sha) {
  stage('Packages') {
    packPackages(ctx, nuget, tag, sha)
    pushPackages(tag)
  }
}

// On the release agent, with no credential at all: the tag checked out, publish.nuget.run in
// publish.nuget.image with run-check.sh (from the checkout's root, RELEASE_TAG and RELEASE_VERSION in its
// environment), then the packages (publish.nuget.packages, default **/*.nupkg) stashed after LINK_GUARD:
// none matching fails the publish, a link leading out of the checkout refuses them.
void packPackages(Map ctx, Map nuget, String tag, String sha) {
  String glob = (nuget.packages ?: NUGET_PACKAGES) as String
  node('release') {
    try {
      checkoutTag(ctx, tag, sha)
      withEnv(["YGG_IMAGE=${nuget.image}", "YGG_RUN=${nuget.run}", "YGG_TMP=${scratch()}", "YGG_TAG=${tag}",
               "YGG_VERSION=${tagVersion(tag)}"]) {
        int code = sh(script: '''
          umask 077
          mkdir -p "$YGG_TMP"
          env_file="$YGG_TMP/nuget-pack.env"
          printf 'RELEASE_TAG=%s\\nRELEASE_VERSION=%s\\n' "$YGG_TAG" "$YGG_VERSION" >"$env_file"
          bash yggdrasil/scripts/run-check.sh nuget-pack "$YGG_IMAGE" "$WORKSPACE/app" --env-file "$env_file" \\
            -- sh -c "$YGG_RUN"
        ''', returnStatus: true)
        if (code != 0) {
          error "publish.nuget.run failed (exit ${code}): nothing is pushed."
        }
      }
      dir('app') {
        refuseLinks(false, 'the packages')
        stashGlob('nuget-packages', glob, 'publish.nuget.packages')
      }
    } finally {
      cleanScratch()
      cleanWs()
    }
  }
}

// On the release agent again, a fresh workspace: this repository's tools and the packages' stash only (no
// link among them), then scripts/nuget-push.sh with the NuGet key bound (withNugetKey), which runs nothing
// but `dotnet nuget push --skip-duplicate` in its pinned image. The feed is nuget.org unless the agent's
// environment names another (YGG_NUGET_SOURCE, and YGG_NUGET_CA_FILE for its CA: the live tests' local
// feed).
void pushPackages(String tag) {
  node('release') {
    try {
      checkoutTools()
      dir('packages') {
        deleteDir()
      }
      sh(script: 'mkdir -p packages')
      dir('packages') {
        unstash 'nuget-packages'
        refuseLinks(true, 'the packages')
      }
      withNugetKey {
        sh(script: '''
          set -- "$WORKSPACE/packages/**/*.nupkg"
          if [ -n "${YGG_NUGET_SOURCE:-}" ]; then set -- "$@" --source "$YGG_NUGET_SOURCE"; fi
          if [ -n "${YGG_NUGET_CA_FILE:-}" ]; then set -- "$@" --ca-file "$YGG_NUGET_CA_FILE"; fi
          bash yggdrasil/scripts/nuget-push.sh "$@"
        ''')
      }
    } finally {
      cleanScratch()
      cleanWs()
    }
  }
  echo "The packages of ${tag} are pushed."
}

// ---- The actions-release job ----

// The actions-release job (platform/jenkins/controller/casc.yaml, its script jenkins/actions-release.groovy
// kept in Jenkins): the Windows files of a release that was published without them (after the skip, or
// a Windows run that failed), attached to it. Parameters REPOSITORY (a catalog id whose yggdrasil.yml at
// the tag declares release.windows) and TAG (one of its version tags, which must exist and be on main:
// readReleaseTag). The workflow is dispatched on the tag and waited for outside node(); its files are
// downloaded on the publish agent and put on the release (and its GitHub copy) with publish-release.sh
// attach. When the GitHub copy is still to make (a release published after the skip, on the git server
// only: attach exits 5), the files go to the git server's release, the build is unstable and says to run
// the release-resume job, which makes the GitHub copy with every file. Under the application's release
// lock, so it never runs next to that application's release.
void actionsRelease() {
  String app = (params.REPOSITORY ?: '').toString().trim()
  String tag = (params.TAG ?: '').toString().trim()
  Map ctx = catalogContext(app)
  if (!isVersionTag(ctx.tags, tag)) {
    error "'${printable(tag)}' is not a version tag of ${app} (${ctx.tags})"
  }
  if (!githubActions(ctx)) {
    error 'This installation has no GitHub mirror (catalog.yaml mirror): there is no GitHub Actions to get Windows files from.'
  }
  requirePublishAgent(ctx)
  int seconds = ((ctx.plan.find { it.trigger == 'release' }?.checksTimeout) ?: 3600) as int
  timeout(time: 4, unit: 'HOURS') {
    withForgeEnvironment(ctx) {
      lock(resource: "release-${app}") {
        Map described = readReleaseTag(ctx, tag)
        Map releaseConfig = described.config?.release as Map
        if (!releaseConfig?.windows) {
          error "${app}'s yggdrasil.yml at ${tag} declares no release.windows: there are no Windows files to attach."
        }
        String problem = releaseProblem(releaseConfig)
        if (problem) {
          error "yggdrasil.yml's release cannot be used: ${problem}"
        }
        // Attached afterwards, Windows files would be outside what release.finalize covered (its manifest,
        // its signature): such a release is only ever made whole, by release-resume.
        if (needsWindowsFiles(releaseConfig)) {
          error "${app}'s yggdrasil.yml at ${tag} declares release.finalize, which must cover the Windows files: they are never attached afterwards. Run the release-resume job (REPOSITORY=${app}, TAG=${tag}), which builds them and runs release.validate and release.finalize on the whole set."
        }
        Map got = null
        stage('Windows files') {
          got = windowsFiles(ctx, releaseConfig.windows as Map, tag, seconds)
        }
        if (got.stashes == null) {
          error "No Windows files for ${tag}: ${got.reason}. Nothing was attached."
        }
        boolean mirrorReleases = mirrorsReleases(ctx)
        Map attached = null
        stage('Attach') {
          attached = onPublishAgent(ctx, true, mirrorReleases) {
            dir('release-files') {
              deleteDir()
            }
            sh(script: 'mkdir -p release-files')
            dir('release-files') {
              for (String name : got.stashes) {
                unstash name
              }
              refuseLinks(true, 'the Windows files')
            }
            List arguments = ['attach', ctx.repo, tag, "${env.WORKSPACE}/release-files"]
            if (mirrorReleases) {
              arguments << '--mirror-releases'
            }
            String tmp = scratch()
            int code = 1
            withEnv(["GITHUB_OWNER=${githubOwner(ctx)}"]) {
              code = toolStatus('publish-release.sh', arguments, "${tmp}/attach.out")
            }
            // 5: attached on the git server; GitHub has no release of the tag yet (its copy is pending).
            if (code != 0 && code != 5) {
              error "publish-release.sh attach exited ${code}; its message is above. The Windows files may be on some of the releases: run this job again."
            }
            return [urls: readScratch(tmp, 'attach.out').readLines().findAll { it.trim() }, pending: code == 5]
          }
          echo "Attached the Windows files of ${tag}: ${attached.urls.join(' ')}"
        }
        if (attached.pending) {
          currentBuild.description = "${app} ${tag}: Windows files attached on the git server; GitHub copy pending"
          unstable "GitHub has no release of ${tag} yet (it was published after the skip, on the git server only): the Windows files are on the git server's release. The release-resume job (REPOSITORY=${app}, TAG=${tag}) makes the GitHub copy, with them."
        } else {
          currentBuild.description = "${app} ${tag}: Windows files attached"
        }
      }
    }
  }
}

// ---- The tag-check job ----

// The tag-check job (platform/jenkins/controller/casc.yaml, its script jenkins/tag-check.groovy kept in
// Jenkins), every day and by hand: whether each release's tag still names the commit it was released from.
// On Forgejo whoever may create a repository's version tags may also move and delete them (the tag
// protection is one list of who may change a tag: the accepted gap, docs/setup.md step 12); this is how a
// moved or deleted one is noticed. For every catalog entry that releases (releasingRepositories), forge.sh
// release-tags lists its published releases with the commit each was released from (its target, which
// create-release sets to the release's commit), and forge.sh tag-sha says where each tag is now:
//   ok              the tag is on the release's commit
//   MOVED           the tag is on another commit
//   DELETED         there is no such tag any more (tag-sha exit 4)
//   unknown target  the release names no commit (a branch: a release made by hand, or an old one), so
//                   whether its tag moved is not known: logged, not a failure (a deleted tag is DELETED)
//
// A deleted tag's release does not stay published: Forgejo (once it has processed the push) and GitHub turn
// it into a draft, which the read credential does not see. So the job remembers: every run archives the
// releases it knows of (TAG_RECORD: those published now and those of the previous run's record), and the
// next run checks the tags of all of them, published or not. A release seen once is checked from then on;
// one deleted before the job first saw it is not noticed. An entry leaves the record with its repository
// leaving the catalog, or when the owner names it in the job's FORGET parameter: one "<repository> <tag>" a
// line, for a release deleted on purpose (a real deletion is repaired instead). Each line is checked against
// the repositories the job visits and TAG before anything runs, and an invalid one fails the build; the
// value is only ever compared in Groovy, never given to a shell.
//
// Any MOVED or DELETED fails the build, with a line each at the end of the log and the count in the build's
// description; so does a repository or a tag that could not be read. The GitHub copies, which nobody can
// move or delete a tag on (rules.py's mirror tags), are what to repair from (docs/setup.md).
//
// On the ci agent with the read credential only: no write token, no GitHub App, and nothing of any
// repository is checked out or run -- the releases and the tags are read through the server's API. Tag
// names and targets come from the server: they reach forge.sh as arguments (toolStatus: environment
// variables, never part of a script), the record through writeFile, and the log through printable().
void tagCheck() {
  Map catalog = loadCatalog()
  List entries = releasingRepositories(catalog)
  // A job made before the parameter existed has no FORGET at all.
  Map forget = forgetLines((params.FORGET ?: '').toString(), entries)
  if (forget.problem) {
    error "${forget.problem}. Nothing was checked."
  }
  Map ctx = [forge: forgeOf(catalog), owner: catalog.owner.toString(),
             repository: (catalog.repository ?: 'yggdrasil').toString()]
  // Read on the controller, before any node: the record the last run that made one archived, without what
  // FORGET names.
  List previous = tagRecordLines(previousTagRecord(currentBuild.rawBuild))
  Map dropped = forgotten(previous, forget.lines as List)
  previous = dropped.kept as List
  for (Map entry : dropped.gone as List) {
    echo "Forgotten: ${entry.repo} ${entry.tag} (released from ${printable(entry.target)}): no longer checked, unless its release is published again."
  }
  for (Map line : dropped.unknown as List) {
    echo "FORGET: ${line.repo} ${line.tag} is not in the record: nothing to forget."
  }
  Map report = null
  timeout(time: 1, unit: 'HOURS') {
    withForgeEnvironment(ctx) {
      stage('Check the release tags') {
        node('ci') {
          try {
            checkoutTools()
            report = checkReleaseTags(entries, previous)
            writeFile file: TAG_RECORD, text: tagRecordText(report.record as List)
            archiveArtifacts artifacts: TAG_RECORD
          } finally {
            cleanScratch()
            cleanWs()
          }
        }
      }
    }
  }
  int checked = report.checked as int
  List problems = report.problems as List
  List unreadable = report.unreadable as List
  if (!problems && !unreadable) {
    currentBuild.description = "${checked} release ${checked == 1 ? 'tag' : 'tags'} checked".toString()
    echo "Every release tag is on the commit it was released from (${currentBuild.description}, in ${entries.size()} ${entries.size() == 1 ? 'repository' : 'repositories'})."
    return
  }
  List parts = []
  if (problems) {
    parts << "${problems.size()} release ${problems.size() == 1 ? 'tag' : 'tags'} moved or deleted"
  }
  if (unreadable) {
    parts << "${unreadable.size()} not checked"
  }
  currentBuild.description = parts.join('; ')
  // A line per problem, together at the end of the log, where the owner looks first.
  for (Object line : problems + unreadable) {
    echo line.toString()
  }
  error "${currentBuild.description}. Repair a moved or deleted tag from its GitHub copy: docs/setup.md, step 12."
}

// Stage Check the release tags, on the ci agent (tagCheck): for every entry, its published releases and
// the ones of the previous record that are not published any more, with the read token. Returns [checked:
// how many tags were compared, problems: a MOVED or DELETED line each, unreadable: a line for each
// repository or tag that could not be read, record: what this run knows of, for the next one].
Map checkReleaseTags(List entries, List previous) {
  String tmp = scratch()
  int checked = 0
  List problems = []
  List unreadable = []
  List record = []
  for (Map entry : entries) {
    String repo = entry.repo
    List remembered = recordOf(previous, repo)
    int code = withForgeToken('read') {
      return toolStatus('forge.sh', ['release-tags', repo], "${tmp}/release-tags")
    }
    if (code != 0) {
      unreadable << "NOT CHECKED: ${repo}: forge.sh release-tags exited ${code} (its message is above)".toString()
      record.addAll(remembered)
      continue
    }
    List published = []
    for (Map release : releaseTagLines(readScratch(tmp, 'release-tags'))) {
      if (release.tag ==~ TAG) {
        published << [repo: repo, tag: release.tag, target: release.target, published: true]
      } else {
        echo "${repo}: skipped the release of '${printable(release.tag)}', which is not a tag name the pipeline makes."
      }
    }
    List checking = published + unpublished(remembered, published)
    if (!checking) {
      echo "${repo}: no published release."
    }
    for (Map release : checking) {
      String tag = release.tag
      String target = release.target
      record << [repo: repo, tag: tag, target: target]
      checked++
      // Not among the published releases any more: a draft (what a deleted tag's release becomes) or gone.
      String unlisted = release.published ? '' : ', and its release is no longer published'
      int found = withForgeToken('read') {
        return toolStatus('forge.sh', ['tag-sha', repo, tag], "${tmp}/tag-sha")
      }
      String known = target ==~ COMMIT ? target : null
      String from = known ?: "'${printable(target)}'"
      if (found == 4) {
        problems << "DELETED: ${repo} ${tag} (released from ${from}): the tag is gone${unlisted}".toString()
        continue
      }
      if (found != 0) {
        unreadable << "NOT CHECKED: ${repo} ${tag}: forge.sh tag-sha exited ${found} (its message is above)".toString()
        continue
      }
      String sha = readScratch(tmp, 'tag-sha').trim()
      if (!(sha ==~ COMMIT)) {
        unreadable << "NOT CHECKED: ${repo} ${tag}: forge.sh tag-sha answered '${printable(sha)}', not a commit".toString()
        continue
      }
      if (known == null) {
        echo "${repo} ${tag}: unknown target: released from '${printable(target)}', not a commit, so whether the tag moved is not known (it is on ${sha.take(7)})."
      } else if (sha != known) {
        problems << "MOVED: ${repo} ${tag} (tag on ${sha}, released from ${known})${unlisted}".toString()
      } else if (release.published) {
        echo "${repo} ${tag}: ok (${sha.take(7)})"
      } else {
        echo "${repo} ${tag}: ok (${sha.take(7)}), but its release is not published (a draft, or deleted): publish it again if that was not meant."
      }
    }
  }
  return [checked: checked, problems: problems, unreadable: unreadable, record: record]
}

// release-tags lines ("<tag>\t<target>") as [tag, target]; a line without a tab has an empty target.
@NonCPS
List releaseTagLines(String text) {
  List releases = []
  for (String line : (text ?: '').readLines()) {
    if (!line.trim()) {
      continue
    }
    List columns = line.split('\t', 2) as List
    releases << [tag: columns[0], target: columns.size() > 1 ? columns[1].trim() : '']
  }
  return releases
}

// The record's entries of one repository, as releases that are not published (until shown otherwise).
@NonCPS
List recordOf(List record, String repo) {
  return record.findAll { it.repo == repo }.collect { [repo: it.repo, tag: it.tag, target: it.target, published: false] }
}

// The remembered releases whose tag is not among the published ones.
@NonCPS
List unpublished(List remembered, List published) {
  Set tags = published.collect { it.tag } as Set
  return remembered.findAll { !(it.tag in tags) }
}

// FORGET's lines as [repo, tag] (blank lines skipped), or the problem with the first line that is not
// "<repository> <tag>" of a repository tag-check visits (entries) and a tag name TAG allows.
@NonCPS
Map forgetLines(String text, List entries) {
  Set repos = entries.collect { it.repo } as Set
  List lines = []
  int number = 0
  for (String line : text.readLines()) {
    number++
    if (!line.trim()) {
      continue
    }
    List columns = line.trim().split(/\s+/) as List
    if (columns.size() != 2 || !repos.contains(columns[0]) || !(columns[1] ==~ TAG)) {
      return [lines: [], problem: "FORGET line ${number} ('${printable(line)}') is not '<repository> <tag>' of a repository this job checks (${repos.sort().join(', ')})".toString()]
    }
    lines << [repo: columns[0], tag: columns[1]]
  }
  return [lines: lines, problem: null]
}

// The record split by FORGET's lines: kept, gone (the entries named), unknown (lines naming no entry).
@NonCPS
Map forgotten(List record, List lines) {
  Set named = lines.collect { "${it.repo} ${it.tag}".toString() } as Set
  Set present = record.collect { "${it.repo} ${it.tag}".toString() } as Set
  return [kept: record.findAll { !named.contains("${it.repo} ${it.tag}".toString()) },
          gone: record.findAll { named.contains("${it.repo} ${it.tag}".toString()) },
          unknown: lines.findAll { !present.contains("${it.repo} ${it.tag}".toString()) }]
}

// The record: one line per release, "<repository>\t<tag>\t<target>" (release-tags escapes a tab or a line
// break in a target, so neither field can split a line).
@NonCPS
String tagRecordText(List record) {
  return record.collect { "${it.repo}\t${it.tag}\t${it.target}" }.join('\n') + (record ? '\n' : '')
}

@NonCPS
List tagRecordLines(String text) {
  List record = []
  for (String line : (text ?: '').readLines()) {
    List columns = line.split('\t', 3) as List
    if (columns.size() == 3 && columns[1] ==~ TAG) {
      record << [repo: columns[0], tag: columns[1], target: columns[2]]
    }
  }
  return record
}

// The record the newest earlier build of this job archived ('' when none did: the first run, or every
// earlier build deleted). On the controller: the build's artifacts are read there.
@NonCPS
String previousTagRecord(def run) {
  for (def build = run?.previousBuild; build != null; build = build.previousBuild) {
    def file = build.artifactManager.root().child(TAG_RECORD)
    if (file.isFile()) {
      return file.open().withCloseable { it.getText('UTF-8') }
    }
  }
  return ''
}

// The repositories whose releases tag-check checks, once each, in catalog order: every deployable
// application with a trigger: release environment (its `repository`, else its id: applications sharing a
// repository are one) and every repositories entry with release: owner-tag -- the entries catalogContext
// calls 'deploy' and 'owner-tag' (pipeline_config.py entry_for's release). [repo] each.
@NonCPS
List releasingRepositories(Map catalog) {
  List repos = []
  for (Map app : (catalog.systems ?: []).collectMany { it.applications ?: [] }.findAll { it.kind != 'platform' }) {
    if (resolve(catalog, app.id as String).any { it.trigger == 'release' }) {
      repos << (app.repository ?: app.id).toString()
    }
  }
  for (Map entry : (catalog.repositories ?: [])) {
    if ((entry.release ?: 'none') == 'owner-tag') {
      repos << entry.id.toString()
    }
  }
  return repos.unique().collect { [repo: it] }
}

// ---- Releases (GitHub, a repository without yggdrasil.yml: the path it always had) ----

// The release pull request: every release environment in order, then merge, package, publish and
// clean up. A packaging failure leaves the release draft private and fails the Jenkins build.
void release(String app, List environments, boolean releaseArtifacts, String repo) {
  if (!environments) {
    echo "${app} has no environment with trigger: release; nothing deploys on this pull request."
    return
  }
  String version = (env.CHANGE_BRANCH =~ RELEASE_BRANCH)[0][1]
  String sha

  stage('Wait for GitHub checks') {
    node(environments[0].agent) {
      try {
        sha = checkoutBuild()
        github(['wait-checks', repo, sha, "${environments[0].checksTimeout}"])
      } finally {
        cleanWs()
      }
    }
    for (Map environment : environments) {
      reportStatus(repo, sha, 'pending', "deploy/${environment.id}", "Waiting to deploy to ${environment.name}")
    }
  }

  for (Map environment : environments) {
    deployTo(app, environment, version, sha, false, repo)
  }

  stage('Merge, package, publish and clean up') {
    node(environments[-1].agent) {
      try {
        checkoutBuild()
        String mergeSha = github(['merge-pr', repo, env.CHANGE_ID, sha, "release: v${version} (#${env.CHANGE_ID})"], true)
        String url = releaseArtifacts
          ? github(['publish-release', repo, version, mergeSha], true)
          : github(['release', repo, version, mergeSha], true)
        github(['delete-branch', repo, env.CHANGE_BRANCH])
        currentBuild.description = "v${version} -> ${url}"
        echo "Released v${version}: ${url}"
      } finally {
        cleanWs()
      }
    }
  }
}

// ---- Deploys ----

// One environment: wait for approval if it asks for it, then deploy on its agent. With a status
// commit (release pull requests), deploy/<environment> reports the outcome on that commit, posted
// after the node. start: leave an onDemand environment running even if it was stopped (DEPLOY_START=1,
// see deploy.sh). inputs: checks whose stashed archives go into the build context (DEPLOY_INPUTS_DIR).
// expectedSha (a push to develop, main or release/*): the commit the checks ran on; the deploy agent's
// checkout must be that commit or nothing is deployed -- a push that lands while the checks run must not
// deploy a commit no check saw. Checked without a status (branch builds post none). note: appended to
// the deploy/<environment> descriptions (" (without GitHub Actions checks)" after the skip).
void deployTo(String app, Map environment, String version, String statusSha, boolean start = false,
              String repo = null, List inputs = [], String expectedSha = null, String note = '') {
  stage("Deploy to ${environment.name}") {
    if (environment.approval) {
      // Outside node(): waiting for a person must not hold an agent.
      input message: "Deploy ${app} ${version} to ${environment.name}?", ok: 'Deploy'
    }
    String sha = null
    def failure = null
    node(environment.agent) {
      try {
        sha = checkoutBuild()
        if (statusSha && sha != statusSha) {
          error "The pull request moved from ${statusSha.take(7)} to ${sha.take(7)} during the release; start it again."
        }
        if (expectedSha) {
          sameCommit(sha, expectedSha)
        }
        String inputsDir = ''
        if (inputs) {
          inputsDir = "${env.WORKSPACE}/deploy-inputs"
          dir('deploy-inputs') {
            for (String name : inputs) {
              unstash "check-${name}"
            }
          }
        }
        withEnv(["DEPLOY_START=${start ? '1' : ''}", "DEPLOY_INPUTS_DIR=${inputsDir}", "YGG_ENVIRONMENT=${environment.id}",
                 "YGG_STACK=${app}", "YGG_VERSION=${version}-${sha.take(7)}"]) {
          sh(script: 'bash yggdrasil/scripts/deploy.sh "$YGG_ENVIRONMENT" "$YGG_STACK" app "$YGG_VERSION"')
        }
      } catch (problem) {
        failure = problem
      } finally {
        cleanWs()
      }
    }
    if (statusSha) {
      if (failure == null) {
        reportStatus(repo, sha, 'success', "deploy/${environment.id}", "Deployed to ${environment.name}${note}")
      } else {
        catchError(buildResult: 'FAILURE', stageResult: 'FAILURE') {
          reportStatus(repo, statusSha, 'failure', "deploy/${environment.id}", "Deploy to ${environment.name} failed${note}")
        }
      }
    }
    if (failure != null) {
      throw failure
    }
  }
}

// ---- Checkouts ----

// On a deploy agent: the application at the build's commit in ./app (checkout scm, with the job's
// own credential, as always), and this repository in ./yggdrasil for its scripts, stack files and
// catalog. Returns the application commit. Never on the ci or release agent: there, checkoutCommit().
String checkoutBuild() {
  dir('app') {
    checkout scm
  }
  checkoutTools()
  return headCommit()
}

// On the ci agent: the application at `sha` in ./app with the read credential only, and this
// repository in ./yggdrasil. Not `checkout scm`: that checks out with the job's branch-source
// credential, which on GitHub is the main GitHub App, and the ci agent runs the repositories' check
// code. A plain git fetch of the whole repository instead: every branch (origin/develop and
// origin/main among them), the pull request's head (refs/pull/<n>/head, so a head the branch has
// already moved past is still there), the tags, and the full history -- what branch-policy.sh compares
// with (on a shallow clone or without the tags it would pass what it should fail) and the base of a
// pull request's diff. The token reaches git through a credential helper reading the environment,
// never an argument or the remote's URL. Returns the commit, checked to be `sha`.
String checkoutCommit(Map ctx, String sha) {
  if (!(sha ==~ COMMIT)) {
    error "checkoutCommit: '${printable(sha)}' is not a commit"
  }
  String change = env.CHANGE_ID ?: ''
  if (!(change ==~ '[0-9]*')) {
    error "checkoutCommit: '${printable(change)}' is not a pull request number"
  }
  withForgeToken('read') {
    withEnv(["YGG_REPO_URL=${env.FORGE_URL}/${env.FORGE_OWNER}/${ctx.repo}.git", "YGG_SHA=${sha}", "YGG_CHANGE_ID=${change}"]) {
      sh(script: '''
        set +x
        # A refused token fails the fetch instead of waiting for someone to type a password.
        export GIT_TERMINAL_PROMPT=0
        helper='!f() { test "$1" = get || exit 0; echo "username=$FORGE_USER"; echo "password=$FORGE_TOKEN"; }; f'
        rm -rf app
        git init --quiet app
        cd app
        git remote add origin "$YGG_REPO_URL"
        set -- '+refs/heads/*:refs/remotes/origin/*'
        if [ -n "$YGG_CHANGE_ID" ]; then
          set -- "$@" "+refs/pull/$YGG_CHANGE_ID/head:refs/remotes/origin/PR-$YGG_CHANGE_ID"
        fi
        git -c credential.helper= -c "credential.helper=$helper" fetch --quiet --tags origin "$@"
        git -c advice.detachedHead=false checkout --quiet --detach "$YGG_SHA"
      ''')
    }
  }
  checkoutTools()
  String head = headCommit()
  sameCommit(head, sha)
  return head
}

// The commit checked out in ./app.
String headCommit() {
  String sha = sh(script: 'git -C app rev-parse HEAD', returnStdout: true).trim()
  if (!(sha ==~ COMMIT)) {
    error "The checkout's HEAD is not a commit: ${printable(sha)}"
  }
  return sha
}

// The commit this build is for, read on the controller without checking anything out: what the ci
// agent then fetches by hash. The first call asks the branch source for the head's revision and records
// it on the build (see revisionHash), so the head at that moment -- not necessarily the commit that
// started the build -- is the build's commit from then on.
String buildCommit() {
  String sha = revisionHash(currentBuild.rawBuild)
  if (!(sha && sha ==~ COMMIT)) {
    error "Cannot tell which commit this build is for: the build has no usable branch-source revision (${printable(sha)})."
  }
  return sha
}

// The hash of the build's revision: a pull request's head (GitHub's PullRequestSCMRevision.pullHash,
// the Gitea plugin's PullRequestSCMRevision.origin.hash), else a branch's or tag's own hash. Duck-typed:
// the two plugins' classes need not both be installed.
//
// The revision is the build's SCMRevisionAction when it has one. The job's factory
// (pipelineBranchDefaultsProjectFactory, which runs the Jenkinsfile kept in Jenkins) never records one
// -- unlike the factory that reads a repository's Jenkinsfile (proved by scripts/test_jenkins_live.py)
// -- so the first call asks the branch source for the head's revision, on the controller with the job's
// own read credential, as that factory would have, and records it on the build, so every later call
// gets the same commit. Whether the deploy agents' `checkout scm` follows the recorded action is not
// relied on: deployTo() compares their checkout with the checked commit (expectedSha).
@NonCPS
String revisionHash(def run) {
  def revision = run.getAction(jenkins.scm.api.SCMRevisionAction)?.revision
  if (revision == null) {
    def head = jenkins.scm.api.SCMHead.HeadByItem.findHead(run.parent)
    def source = jenkins.scm.api.SCMSource.SourceByItem.findSource(run.parent)
    if (head == null || source == null) {
      return null
    }
    revision = source.fetch(head, hudson.model.TaskListener.NULL)
    if (revision == null) {
      return null
    }
    run.addAction(new jenkins.scm.api.SCMRevisionAction(source, revision))
  }
  if (revision.hasProperty('pullHash')) {
    return revision.pullHash as String
  }
  if (revision.hasProperty('origin') && revision.origin != null && revision.origin.hasProperty('hash')) {
    return revision.origin.hash as String
  }
  if (revision.hasProperty('hash')) {
    return revision.hash as String
  }
  return null
}

// This repository in ./yggdrasil, from the same git server with the read credential: its code (scripts/,
// everything the library calls) at the version this build loaded the library at (libraryVersion(): the
// pinned YGGDRASIL_LIBRARY_VERSION tag), so the library and its scripts are always one release; and the
// owner's data (OWNER_DATA: catalog.yaml and stacks/) from main, which is where the owner keeps them up
// to date. A path main does not have keeps the version's copy.
void checkoutTools() {
  withForgeToken('read') {
    withEnv(["YGG_TOOLS_URL=${env.FORGE_URL}/${env.GITHUB_OWNER}/${env.YGGDRASIL_REPOSITORY}.git",
             "YGG_TOOLS_VERSION=${libraryVersion()}", "YGG_OWNER_DATA=${OWNER_DATA.join(' ')}"]) {
      sh(script: '''
        set +x
        export GIT_TERMINAL_PROMPT=0
        helper='!f() { test "$1" = get || exit 0; echo "username=$FORGE_USER"; echo "password=$FORGE_TOKEN"; }; f'
        fetch() { git -c credential.helper= -c "credential.helper=$helper" fetch --quiet --depth 1 origin "$@"; }
        rm -rf yggdrasil
        git init --quiet yggdrasil
        cd yggdrasil
        git remote add origin "$YGG_TOOLS_URL"
        # A release tag (what the controller pins); a branch only if there is no such tag.
        fetch "refs/tags/$YGG_TOOLS_VERSION" 2>/dev/null || fetch "refs/heads/$YGG_TOOLS_VERSION"
        git -c advice.detachedHead=false checkout --quiet --detach FETCH_HEAD
        fetch refs/heads/main
        for path in $YGG_OWNER_DATA; do
          if [ -n "$(git ls-tree --name-only FETCH_HEAD -- "$path")" ]; then
            rm -rf "$path"
            git checkout --quiet FETCH_HEAD -- "$path"
          fi
        done
      ''')
    }
  }
}

// The version (a yggdrasil release tag) this build loaded the library at: what Jenkins records as
// library.yggdrasil.version, from casc's defaultVersion (YGGDRASIL_LIBRARY_VERSION).
String libraryVersion() {
  String version = env['library.yggdrasil.version']
  if (!(version ==~ LIBRARY_VERSION) || version.contains('..')) {
    error "Cannot tell which version of the yggdrasil library this build loaded (library.yggdrasil.version is '${printable(version)}')."
  }
  return version
}

// The checkout of an earlier step and this one must be the same commit (every step of a build works on
// the one revision the build started with; this says so loudly if that ever changes).
void sameCommit(String sha, String expected) {
  if (sha != expected) {
    error "The commit moved from ${expected.take(7)} to ${sha.take(7)} during the build; start it again."
  }
}

// ---- Credentials and the git server ----

// The Jenkins credential id for an access to the git server (platform/jenkins/controller/casc.*.yaml):
//   read    checkouts, statuses read, history: forge-read (the read-only jenkins-ci user) on Forgejo;
//           on GitHub github-read, the read-only GitHub App (casc.github-read.yaml), when it is
//           configured, else github-app with a warning in the log
//   write   statuses: forge-write (forge.jenkinsUser), bound by reportStatus() only, on the controller
//   github  the GitHub App: GitHub Actions on the mirror (on the publish agent), and the release path's
//           GitHub commands
// GitHub installations have no forge-* credentials: the GitHub App (and the optional read-only one) is
// everything there.
String forgeCredential(String access) {
  boolean forgejo = env.FORGE_TYPE == 'forgejo'
  if (access == 'read') {
    if (forgejo) {
      return 'forge-read'
    }
    // Looked up once per build; the answer is kept in the build's environment.
    if (env.YGG_GITHUB_READ == null) {
      env.YGG_GITHUB_READ = credentialExists('github-read') ? 'yes' : 'no'
      if (env.YGG_GITHUB_READ == 'no') {
        echo 'github-read is not configured: the CI agent uses the GitHub App'
      }
    }
    return env.YGG_GITHUB_READ == 'yes' ? 'github-read' : 'github-app'
  }
  if (access == 'write') {
    return forgejo ? 'forge-write' : 'github-app'
  }
  if (access == 'github') {
    return 'github-app'
  }
  error "forgeCredential: unknown access '${access}' (read, write or github)"
}

// Whether a global credential with this id exists (the library runs unsandboxed, on the controller).
@NonCPS
boolean credentialExists(String id) {
  List found = com.cloudbees.plugins.credentials.CredentialsProvider.lookupCredentials(
    com.cloudbees.plugins.credentials.common.StandardCredentials.class, jenkins.model.Jenkins.get(), hudson.security.ACL.SYSTEM,
    Collections.emptyList())
  return found.any { it.id == id }
}

// Binds the token of forgeCredential(access) for the body: as GH_TOKEN for 'github' (github.sh), else
// FORGE_TOKEN (forge.sh), with its user name in FORGE_USER. Returns the body's value.
def withForgeToken(String access, Closure body) {
  String variable = access == 'github' ? 'GH_TOKEN' : 'FORGE_TOKEN'
  return withCredentials([usernamePassword(credentialsId: forgeCredential(access), usernameVariable: 'FORGE_USER',
                                           passwordVariable: variable)]) {
    return body()
  }
}

// The NuGet API key (NUGET_CREDENTIAL, casc.nuget.yaml) as NUGET_API_KEY for the body. Only pushPackages
// binds it, on the release agent, around scripts/nuget-push.sh (scripts/test_pipeline_library.py checks it).
def withNugetKey(Closure body) {
  return withCredentials([string(credentialsId: NUGET_CREDENTIAL, variable: 'NUGET_API_KEY')]) {
    return body()
  }
}

// Sets a commit status on the git server: the one place statuses are posted, and the only user of the
// write credential. Runs on the controller (httpRequest outside any node(): inside one, the request
// would run on that agent, with the token), the token masked in the request's header. The same body
// on the same path for Forgejo and GitHub (as forge.sh set-status). The description is reduced to
// printable ASCII and 140 characters: it can carry text from a branch (a reason, an error message). A
// context the forges would not show as a check name is refused. Tries three times on a server error.
void reportStatus(String repo, String sha, String state, String context, String description, String targetUrl = null) {
  if (!(state in ['pending', 'success', 'failure', 'error'])) {
    error "reportStatus: unknown state '${state}'"
  }
  if (!(sha ==~ COMMIT)) {
    error "reportStatus: '${printable(sha)}' is not a commit"
  }
  if (!(context ==~ STATUS_CONTEXT)) {
    error "reportStatus: '${printable(context)}' cannot be a status context"
  }
  String target = targetUrl && targetUrl ==~ WEB_URL ? targetUrl : env.BUILD_URL
  Map body = [state: state, context: context, description: safeText(description)]
  if (target) {
    body.target_url = target
  }
  boolean github = env.FORGE_TYPE == 'github'
  String url = "${env.FORGE_API_URL}/repos/${pathSegment(env.FORGE_OWNER)}/${pathSegment(repo)}/statuses/${sha}"
  echo "Status ${context} on ${sha.take(7)}: ${state} (${body.description})"
  withForgeToken('write') {
    String answer = ''
    for (int attempt = 1; attempt <= 3; attempt++) {
      int status = 0
      try {
        def response = httpRequest(url: url, httpMode: 'POST', contentType: 'APPLICATION_JSON',
                                   requestBody: JsonOutput.toJson(body),
                                   customHeaders: [[name: 'Authorization', value: "${github ? 'Bearer' : 'token'} ${env.FORGE_TOKEN}", maskValue: true],
                                                   [name: 'Accept', value: github ? 'application/vnd.github+json' : 'application/json']],
                                   validResponseCodes: '100:599', consoleLogResponseBody: false, quiet: true, timeout: 30)
        status = response.status
        if (status >= 200 && status < 300) {
          return
        }
        answer = "HTTP ${status}: ${safeText(response.content, 300)}"
      } catch (FlowInterruptedException interruption) {
        throw interruption
      } catch (Exception problem) {
        answer = safeText(problem.message, 300)
      }
      if (status >= 400 && status < 500) {
        break
      }
      if (attempt < 3) {
        sleep time: 5, unit: 'SECONDS'
      }
    }
    error "Could not set the status ${context} on ${sha.take(7)}: ${answer}"
  }
}

// Runs scripts/github.sh (GitHub Actions, and on GitHub the server's commands through forge.sh) with
// the GitHub App's installation token, which only ever reaches the shell through its environment
// (GH_TOKEN). arguments: a list, each one argument however it is spelled.
String github(List arguments, boolean returnStdout = false) {
  return withForgeToken('github') {
    return tool('github.sh', arguments, returnStdout)
  }
}

// yggdrasil/scripts/<name> with arguments (see TOOL), from the workspace's root.
String tool(String name, List arguments, boolean returnStdout = false) {
  return withEnv(toolEnvironment(name, arguments, '')) {
    return sh(script: TOOL, returnStdout: returnStdout)?.toString()?.trim()
  }
}

// The same, returning the exit status; stdout goes to the file `out`.
int toolStatus(String name, List arguments, String out) {
  return withEnv(toolEnvironment(name, arguments, out)) {
    return sh(script: TOOL, returnStatus: true)
  }
}

@NonCPS
List toolEnvironment(String name, List arguments, String out) {
  List variables = ["YGG_TOOL=${name}".toString(), "YGG_ARGC=${arguments.size()}".toString(), "YGG_OUT=${out}".toString()]
  arguments.eachWithIndex { argument, index -> variables << "YGG_ARG_${index + 1}=${argument}".toString() }
  return variables
}

// ---- Scratch files: in the workspace's @tmp, next to it, never inside a check's mount ----

String scratch() {
  return "${env.WORKSPACE}@tmp/yggdrasil"
}

String readScratch(String tmp, String name) {
  String text = ''
  dir(tmp) {
    if (fileExists(name)) {
      text = readFile(name)
    }
  }
  return text
}

void cleanScratch() {
  dir(scratch()) {
    deleteDir()
  }
}

// ---- Small helpers ----

@NonCPS
def parseJson(String text) {
  // JsonSlurperClassic: plain HashMaps and ArrayLists, which a pipeline can keep across a restart.
  return new JsonSlurperClassic().parseText(text)
}

// What a plan must look like before anything of it is used (pipeline_config.py checks the same; this
// is the library's own look, next to where the values go). null when fine.
@NonCPS
String planProblem(List planned, Map entry) {
  for (Object item : planned) {
    if (!(item instanceof Map)) {
      return 'an entry is not an object'
    }
    Map check = item as Map
    if (!(check.name instanceof String) || !(check.name ==~ CHECK_NAME)) {
      return "'${printable(check.name)}' is not a check name"
    }
    if (!(check.action in ['run', 'reuse', 'skip'])) {
      return "${check.name}: unknown action '${printable(check.action)}'"
    }
    if (!(check.image instanceof String) || !(check.image ==~ IMAGE)) {
      return "${check.name}: '${printable(check.image)}' is not an image"
    }
    if (!(check.run instanceof String) || !check.run.trim()) {
      return "${check.name}: no command to run"
    }
    if (check.credentials != null && !(check.credentials instanceof Map)) {
      return "${check.name}: credentials is not a map"
    }
    for (Object variable : (check.credentials ?: [:]).keySet()) {
      if (!(variable instanceof String) || !(variable ==~ ENV_VAR)) {
        return "${check.name}: '${printable(variable)}' is not an environment variable name"
      }
      if (!(check.credentials[variable] in (entry.credentials ?: []))) {
        return "${check.name}: credential '${printable(check.credentials[variable])}' is not allowed for ${entry.id}"
      }
    }
  }
  return null
}

// The checks this job needs (pipeline_config.py needed_checks): its catalog entry's `checks` (on the
// releasing job's release pull request, `releaseChecks`: every entry's of the repository), then, when the
// build deploys, the deploy.inputs not among them. Each once, in that order.
@NonCPS
List neededChecks(Map entry, Map config, boolean deploying, List releaseChecks = null) {
  List names = new ArrayList((releaseChecks != null ? releaseChecks : (entry.checks ?: [])) as List)
  if (deploying) {
    names.addAll(((config.deploy ?: [:]).inputs ?: []) as List)
  }
  return names.unique()
}

// The checks of yggdrasil.yml the plan does not hold, sorted (the parsed document's maps keep no order),
// printable and comma separated ('' for none): another catalog entry's of the repository (or no one's:
// describe warned). For the log only.
@NonCPS
String notNeeded(Map config, List planned) {
  List names = planned.collect { it.name }
  List others = new ArrayList(((config.checks ?: [:]) as Map).keySet()).findAll { !(it in names) }
  return others.collect { printable(it) }.sort().join(', ')
}

// Whether the plan covers the job's checks exactly (neededChecks): each once, and nothing else. A check the
// plan left out would never run and never report, and a required status that never comes only blocks the
// merge -- on a push it would deploy unchecked, and a deploy input's archive would be missing. A check it
// adds would run, and post a status, for another job. null when fine.
@NonCPS
String planCoverage(List planned, List wanted) {
  List names = planned.collect { it.name }
  List missing = wanted.findAll { !(it in names) }
  if (missing) {
    return "the plan leaves out ${missing.collect { printable(it) }.join(', ')}"
  }
  List extra = names.findAll { !(it in wanted) }
  if (extra) {
    return "the plan has checks this job does not need: ${extra.collect { printable(it) }.join(', ')}"
  }
  List twice = names.findAll { name -> names.count(name) > 1 }.unique()
  if (twice) {
    return "the plan has ${twice.collect { printable(it) }.join(', ')} more than once"
  }
  return null
}

// The checks whose archives go into the image build: deploy.inputs, those that have an archive.
@NonCPS
List deployInputs(Map config) {
  List names = ((config.deploy ?: [:]).inputs ?: []) as List
  Map checks = (config.checks ?: [:]) as Map
  return names.findAll { name -> checks[name] instanceof Map && checks[name].archive }
}

// actions-status lines (<name> TAB <state> TAB <url>) of the names asked about: name -> [state, url].
@NonCPS
Map actionsResults(String out, List names) {
  Map results = [:]
  for (String line : (out ?: '').readLines()) {
    List columns = line.split('\t', -1) as List
    if (columns.size() >= 2 && columns[0] in names) {
      results[columns[0]] = [state: columns[1], url: columns.size() > 2 && columns[2] ==~ WEB_URL ? columns[2] : null]
    }
  }
  return results
}

// Text that may come from a branch, for a status description: printable ASCII only, spaces collapsed,
// at most `max` characters.
@NonCPS
String safeText(Object text, int max = 140) {
  String clean = (text == null ? '' : text.toString()).replaceAll('[^\\x20-\\x7E]+', ' ').replaceAll(' {2,}', ' ').trim()
  return clean.length() <= max ? clean : clean.take(max - 3) + '...'
}

// Text that may come from a branch, for the build log: lines kept, every other control character
// (an escape sequence, a console note) replaced.
@NonCPS
String printable(Object text) {
  String clean = (text == null ? '' : text.toString()).replaceAll('[^\\x20-\\x7E\\n\\t]', '?')
  return clean.length() <= 4000 ? clean : clean.take(4000) + '...'
}

@NonCPS
String bulleted(List lines) {
  return lines.collect { "  - ${printable(it)}" }.join('\n')
}

@NonCPS
String lastLine(String text) {
  List lines = (text ?: '').readLines().findAll { it.trim() }
  return lines ? lines[-1] : null
}

@NonCPS
String pathSegment(String text) {
  return URLEncoder.encode(text, 'UTF-8').replace('+', '%20')
}

// ---- The catalog. Mirrors scripts/catalog.py (DEFAULTS, resolve, forge, mirror): keep the two in step. ----

@NonCPS
String catalogText() {
  return new File(CATALOG).getText('UTF-8')
}

@NonCPS
Map loadCatalog() {
  return new org.yaml.snakeyaml.Yaml().load(catalogText()) as Map
}

@NonCPS
Map findApplication(Map catalog, String app) {
  return catalog.systems.collectMany { it.applications ?: [] }.find { it.id == app } as Map
}

@NonCPS
Map findRepositoryEntry(Map catalog, String id) {
  return (catalog.repositories ?: []).find { it.id == id } as Map
}

// The branching model of a git repository, "release" or "basic" (catalog.py repository_model, which describe
// reports as entry.rules): release when an application on it releases (a trigger: release environment), else
// the `repositories` entry's `rules` (default release), else basic -- applications that only deploy on
// branches. "release" for a repository the catalog does not have.
@NonCPS
String repositoryModel(Map catalog, String repository) {
  List applications = catalog.systems.collectMany { it.applications ?: [] }
    .findAll { it.kind != 'platform' && (it.repository ?: it.id) == repository }
  if (applications.any { app -> resolve(catalog, app.id as String).any { it.trigger == 'release' } }) {
    return 'release'
  }
  Map entry = findRepositoryEntry(catalog, repository)
  if (entry != null) {
    return (entry.rules ?: 'release').toString()
  }
  return applications ? 'basic' : 'release'
}

// The branches a pull request into, or a push to, gets the branch policy and the checks: develop and main,
// and under the basic rules master too -- a fork's default branch, treated like main (rules.py protects it
// the same way, and the Job DSL discovers it). release/* pushes are checked under either model.
@NonCPS
List checkedBranches(String rules) {
  return rules == 'basic' ? ['develop', 'main', 'master'] : ['develop', 'main']
}

// The git server: Forgejo at forge.url, or GitHub without a forge block, and the publish agent
// (catalog.py forge()).
@NonCPS
Map forgeOf(Map catalog) {
  Map block = (catalog.forge ?: [:]) as Map
  Map forge = [type: 'github', url: 'https://github.com', api: 'https://api.github.com']
  if ((block.type ?: 'github') == 'forgejo') {
    String url = block.url.toString().trim().replaceAll('/+$', '')
    forge = [type: 'forgejo', url: url, api: url + '/api/v1']
  }
  forge.publishAgent = block.publishAgent ? block.publishAgent.toString() : releaseAgent(catalog)
  return forge
}

// The agent of the last trigger: release environment over the deployable applications, in catalog
// order (catalog.py _release_agent: the agent that merges and releases today), or null.
@NonCPS
String releaseAgent(Map catalog) {
  String agent = null
  for (Map app : (catalog.systems ?: []).collectMany { it.applications ?: [] }.findAll { it.kind != 'platform' }) {
    for (Map environment : resolve(catalog, app.id as String)) {
      if (environment.trigger == 'release') {
        agent = environment.agent
      }
    }
  }
  return agent
}

// The Forgejo -> GitHub mirror ([github: <owner>, releases: whether releases get a GitHub copy, default
// true]), or null (catalog.py mirror()).
@NonCPS
Map mirrorOf(Map catalog) {
  if (!catalog.mirror) {
    return null
  }
  return [github: catalog.mirror.github.toString(), releases: catalog.mirror.releases == null ? true : catalog.mirror.releases == true]
}

@NonCPS
List resolve(Map catalog, String app) {
  Map defaults = [mode: 'proxy', trigger: 'manual', branches: [], agent: null, approval: false,
                  waitTimeout: 300, keepImages: 3, checksTimeout: 3600, hostSuffix: '', onDemand: false]
  Map application = catalog.systems.collectMany { it.applications ?: [] }.find { it.id == app }
  if (application == null) {
    throw new IllegalArgumentException("'${app}' is not an application in ${CATALOG}")
  }
  Map overrides = application.environments
  List plan = []
  for (Map environment : catalog.environments) {
    if (overrides != null && !overrides.containsKey(environment.id)) {
      continue
    }
    Map resolved = [id: environment.id, name: environment.name]
    defaults.each { key, value ->
      resolved[key] = value
      if (environment.containsKey(key)) resolved[key] = environment[key]
      Map override = overrides?.get(environment.id) ?: [:]
      if (override.containsKey(key)) resolved[key] = override[key]
    }
    resolved.branches = resolved.branches instanceof String ? [resolved.branches] : (resolved.branches ?: [])
    resolved.agent = resolved.agent ?: environment.id
    plan << resolved
  }
  return plan
}

// The version a release tag names, RELEASE_VERSION and YGG_VERSION: after the last `@` of a
// <package>@<version> tag (ArturRios.Data.Sqlite@3.1.0 is 3.1.0), else the tag without a leading v (v1.4.0
// and 1.4.0 are both 1.4.0).
@NonCPS
String tagVersion(String tag) {
  int at = tag.lastIndexOf('@')
  return at >= 0 ? tag.substring(at + 1) : tag.replaceFirst('^v', '')
}

// What `git describe --match` looks for, for the tag before `tag`: the same package's tags (<package>@*) for a
// <package>@<version> tag, so a package's notes never start from another package's release; else `tags`, the
// entry's pattern.
@NonCPS
String previousTagPattern(String tag, String tags) {
  int at = tag.lastIndexOf('@')
  return at >= 0 ? tag.substring(0, at) + '@*' : tags
}

// Whether `tag` is a version tag of an entry whose pattern is `tags`: a tag the pipeline takes (TAG), matching the
// pattern, and naming a version (tagVersion not empty). A pattern such as *@* also matches ArturRios.Data.Sqlite@,
// whose RELEASE_VERSION would be empty, and v* matches v: neither is a version, whatever the catalog says.
@NonCPS
boolean isVersionTag(String tags, String tag) {
  return tag ==~ TAG && globMatches(tags, tag) && !tagVersion(tag).isEmpty()
}

// Whether `text` matches a catalog tag pattern (a glob with * and [classes], as catalog.py's TAG_PATTERN
// allows; the Job DSL's globToRegex reads it the same way).
@NonCPS
boolean globMatches(String glob, String text) {
  StringBuilder regex = new StringBuilder()
  boolean inClass = false
  for (String c : glob.collect { it }) {
    if (inClass) {
      regex.append(c)
      inClass = c != ']'
    } else if (c == '*') {
      regex.append('.*')
    } else if (c == '?') {
      regex.append('.')
    } else if (c == '[') {
      regex.append(c)
      inClass = true
    } else {
      regex.append(java.util.regex.Pattern.quote(c))
    }
  }
  return text ==~ regex.toString()
}

@NonCPS
boolean matchesAny(List globs, String branch) {
  return globs.any { glob ->
    String regex = glob.collect { c -> c == '*' ? '.*' : c == '?' ? '.' : java.util.regex.Pattern.quote(c) }.join('')
    branch ==~ regex
  }
}
