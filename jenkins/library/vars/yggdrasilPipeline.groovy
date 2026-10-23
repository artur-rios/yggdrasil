// The pipeline of every repository Jenkins builds. Every job runs the one Jenkinsfile kept in Jenkins
// (jenkins/Jenkinsfile.default, never the repository's own):
//
//     @Library('yggdrasil') _
//     yggdrasilPipeline()
//
// The job's name is the catalog id (the Job DSL in platform/jenkins/controller/casc.yaml names each
// multibranch job after an application or a `repositories` entry); `stack:`, what older Jenkinsfiles
// passed, still wins when given. What a build deploys, and where, comes from catalog.yaml (mounted on
// the controller, the same file the Job DSL reads): the application's environments, each with its
// options resolved -- default, then the environment's value, then the application's override.
// docs/catalog.md has the options. What a build checks comes from the repository's yggdrasil.yml
// (data, never code: see scripts/pipeline_config.py), read on the ci agent before anything else.
//
//   pull request into develop or main (with yggdrasil.yml)
//                            -> Branch policy (scripts/branch-policy.sh, then the file's branchPolicy)
//                               and Checks (each planned check in parallel on the ci agent), each
//                               reported as a commit status; the build fails once all have reported if
//                               any failed. Into develop on Forgejo with a GitHub mirror, the entry's
//                               actionsChecks are then copied from GitHub Actions (see
//                               copyActionsChecks)
//   PR release/x.y.z -> main -> (with yggdrasil.yml: branch policy and checks first, under the same
//                               lock) wait for the entry's actionsChecks on GitHub Actions (the skip
//                               when they don't finish), then deploy the head to each trigger: release
//                               environment in catalog order, setting deploy/<environment> (required
//                               on main) after each; then, on the publish agent, merge the pull request
//                               and tag vx.y.z; build the release files on the release agent (and the
//                               Windows ones on GitHub), validate them there, and publish the release
//                               on the git server (and its GitHub copy) with publish-release.sh (see
//                               releasePullRequest). On GitHub, a repository without yggdrasil.yml keeps
//                               the path it always had (release(): every GitHub Actions check waited
//                               for, releaseArtifacts: true's artifact workflow)
//   the actions-release job  -> the Windows files of a release published without them (actionsRelease)
//   push to develop, main or release/* (with yggdrasil.yml)
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
// and GitHub alike) and github.sh (GitHub Actions). YGG_GITHUB_SH, in an agent's environment (set by
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
//     release agent's builds only the ids release.build.credentials names (allow-listed like a
//     check's). Files cross between the agents as stashes;
//   - nothing waits on an agent: GitHub Actions, a Windows run, a tag reaching GitHub and the skip are
//     waited for outside node(), each look a short call on the publish agent (pollGithub);
//   - a check binds only the ids its plan names, each one checked against the catalog entry's
//     `credentials` again here, and never one of the forge's own (withCheckCredentials);
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
// A tag of a release (v1.4.0, 1.4.0), before it reaches git or the forge.
@Field static final String TAG = '[A-Za-z0-9][A-Za-z0-9._-]{0,99}'
// A release file glob (release.build.files, release.windows.files) as the stash step takes it: Ant
// patterns (*, **, ?), relative, with no comma or whitespace (stash splits its includes on them); a
// `.` or `..` segment is refused separately (releaseProblem).
@Field static final String RELEASE_GLOB = '[A-Za-z0-9_.*?+@=][A-Za-z0-9_.*?/+@=-]{0,199}'
// A GitHub workflow file (release.windows.workflow).
@Field static final String WORKFLOW = '[A-Za-z0-9][A-Za-z0-9._-]{0,99}\\.ya?ml'
// The skip (the design's words): asked when the GitHub Actions checks of a release did not finish.
@Field static final String SKIP_QUESTION = "GitHub Actions checks didn't finish. Deploy without them?"
@Field static final String SKIP_BUTTON = 'Deploy without GitHub Actions checks'
@Field static final String SKIPPED = 'without GitHub Actions checks'
// delete-branch exit 3: Forgejo keeps a protected branch, which is not a reason to fail a release.
@Field static final String KEPT_BRANCH = 'kept: release branches are protected on Forgejo; the owner deletes them'

// Run from a directory: exit 3, naming them, when a symbolic link under it leads out of it -- or, with
// YGG_ANY_LINK set, when there is any link at all (a release's files are files). A build or a check ran as
// root and can leave a link to anything on the agent (its own files, another workspace, @tmp with the env
// files); archiveArtifacts can be told not to follow links, but junit, stash and the uploads cannot, so
// this runs before them. .git is pruned (the Ant default excludes keep it out of every stash anyway).
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
      if (env.TAG_NAME) {
        // Owner-tagged releases are a later version of this library; a tag builds nothing here yet.
        echo "Nothing to do for the tag ${printable(env.TAG_NAME)}: this library version does not release tags."
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
// its first environment), plan (its environments, resolved), releaseArtifacts, tags (its version tag
// pattern), owner, repository (this repository's name)]. Fails unless `app` is a catalog id of an
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
  return [app: app, repo: application != null ? (application.repository ?: app) : app, forge: forgeOf(catalog),
          mirror: mirrorOf(catalog), checksTimeout: (plan ? plan[0].checksTimeout : 3600) as int, plan: plan,
          releaseArtifacts: application?.releaseArtifacts == true,
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
      Map state = prepare(ctx, 'pull-request')
      if (state.config != null) {
        List failed = verify(ctx, state)
        if (failed) {
          error "Not releasing: ${failed.join(', ')} failed."
        }
      }
      List environments = plan.findAll { it.trigger == 'release' }
      if (ctx.forge.type == 'github' && state.config == null) {
        // A GitHub installation's repository without yggdrasil.yml releases exactly as it always did.
        release(app, environments, releaseArtifacts, ctx.repo)
      } else {
        releasePullRequest(ctx, state, environments)
      }
    }
    return
  }
  if (!(target in ['develop', 'main'])) {
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
  if (!deployToParameter && (branch in ['develop', 'main'] || branch.startsWith('release/'))) {
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
// for a pull request into develop or main or a push to develop, main or release/*, plans its checks.
// Returns [sha, entry, config (null without yggdrasil.yml), plan (null when not planned)]. A file the
// catalog does not allow (an unknown field, a credential not in the entry's `credentials`, ...) fails
// the build here, before anything of it runs.
//
// mode: 'pull-request' (changes against the target branch) or 'push' (against the last commit whose
// checks passed). deploying: the build deploys afterwards, so the checks named in deploy.inputs run
// even when they could be reused or skipped -- their archive goes into the image.
Map prepare(Map ctx, String mode, boolean deploying = false) {
  // The build's commit, known before any checkout: every ci step fetches exactly this one.
  Map state = [sha: buildCommit()]
  stage('Read yggdrasil.yml') {
    node('ci') {
      try {
        checkoutCommit(ctx, state.sha)
        Map document = describeCheckout(ctx)
        state.entry = document.entry
        state.config = document.config
        if (state.config != null) {
          String tmp = scratch()
          withEnv(["YGG_TMP=${tmp}", "YGG_JOB=${ctx.app}", "YGG_CATALOG=${tmp}/catalog.yaml"]) {
            state.plan = planChecks(ctx, state, mode, deploying)
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
    described = [entry: document.entry, config: document.config]
  }
  return described
}

// Inside prepare(): the commit's statuses (who already checked what) and the changed files, then
// pipeline_config.py plan. Returns the plan, checked again here (planProblem).
List planChecks(Map ctx, Map state, String mode, boolean deploying) {
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
           "YGG_BRANCH=${branch}"]) {
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
      python3 yggdrasil/scripts/pipeline_config.py plan "$YGG_JOB" app --statuses "$YGG_TMP/statuses" \\
        --changed "$changed" --branch "$YGG_BRANCH" >"$YGG_TMP/plan.json"
    ''', returnStatus: true)
    if (code != 0) {
      error "pipeline_config.py plan failed (exit ${code}); its message is above."
    }
  }
  List planned = parseJson(readScratch(scratch(), 'plan.json')) as List
  String problem = planProblem(planned, state.entry) ?: planCoverage(planned, state.config)
  if (problem) {
    error "The plan of the checks is not usable: ${problem}"
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
// YGG_CHECKED_COMMIT), or null: a push's changes are counted from there. Looks back 20 successful
// builds at most; a build that only deployed (DEPLOY_TO, no yggdrasil.yml yet) has no commit and is
// passed over.
String lastCheckedCommit() {
  def build = currentBuild.previousSuccessfulBuild
  for (int looked = 0; build != null && looked < 20; looked++) {
    String sha = build.buildVariables?.YGG_CHECKED_COMMIT
    if (sha && sha ==~ COMMIT) {
      return sha
    }
    build = build.previousSuccessfulBuild
  }
  return null
}

// ---- Branch policy ----

// Stage Branch policy, on a pull request into develop or main: scripts/branch-policy.sh, then the
// file's own branchPolicy (in its image, with HEAD_REF, BASE_REF, HEAD_SHA and YGGDRASIL_DIR, the
// yggdrasil checkout's path, in its environment). Reported as branch-policy, never reused. Returns
// whether it passed.
boolean branchPolicy(Map ctx, Map state) {
  boolean passed = false
  String reason = 'The branch policy could not run; see the build'
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
      echo 'yggdrasil.yml has no checks.'
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
        withCheckCredentials(check, state.entry) { String envFile ->
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
  return passed
}

// A check's credentials ({ENV: credential id}, from its plan) bound for the body only, written to a
// file only the agent's user can read (umask 077, in the workspace's @tmp, outside every mount), handed
// to the body as its path ('' when the check has none) and deleted afterwards. Every id is checked
// against the catalog entry's `credentials` here, right before it is bound, and the forge's own ids
// (any id starting with forge- or github-) are refused whatever the catalog says: a check runs code
// from a branch.
void withCheckCredentials(Map check, Map entry, Closure body) {
  Map credentials = (check.credentials ?: [:]) as Map
  if (credentials.isEmpty()) {
    body('')
    return
  }
  String branch = env.CHANGE_BRANCH ?: env.BRANCH_NAME
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
    if (!(id in (entry.credentials ?: []))) {
      error "Check ${check.name}: the credential '${printable(id)}' is not in the catalog's credentials of ${entry.id}"
    }
    // The pipeline's own: forge-read, forge-write, forge-webhook, github-app, github-read.
    if (id.startsWith('forge-') || id.startsWith('github-')) {
      error "Check ${check.name}: '${id}' is named like one of the pipeline's own credentials (forge-*, github-*), never a check's"
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
//   2. every trigger: release environment in catalog order, deploy/<environment> after each (with
//      "without GitHub Actions checks" after a skip);
//   3. on the publish agent: the merge (titled "release: v<version> (#N)", which release_notes.py
//      leaves out of the notes) and the tag v<version> on the merge commit;
//   4. with yggdrasil.yml's release: the release files (releaseFiles: built on the release agent,
//      the Windows ones from GitHub, validated on the release agent);
//   5. on the publish agent: the notes and publish-release.sh (publishRelease), which makes the
//      drafts, uploads and checks the files and publishes the release (Forgejo, then GitHub last);
//   6. the release branch deleted -- kept, with a note, where Forgejo protects it.
// The build's description is v<version> (" (deployed without GitHub Actions checks)" after a skip); it
// is unstable when Windows files are missing (published without them, listed in the notes and the
// log; the actions-release job attaches them later).
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
  for (Map environment : environments) {
    deployTo(app, environment, version, state.sha, false, ctx.repo, [], null, note)
  }

  String merged = mergeAndTag(ctx, state.sha, version, tag)
  Map files = releaseConfig != null ? releaseFiles(ctx, state.entry, releaseConfig, tag, merged, skipped, seconds) : [stash: null, missing: []]
  List urls = publishRelease(ctx, state.entry, version, tag, merged, files)
  currentBuild.description = "${tag}${skipped ? ' (deployed without GitHub Actions checks)' : ''}"
  echo "Released ${tag}: ${urls.join(' ')}"
  if (files.missing) {
    unstable "${tag} was published without its Windows files: ${files.missing.join(', ')} (${files.reason}). The actions-release job attaches them."
  }
  deleteReleaseBranch(ctx)
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
      ensureTag(ctx, tag, commit)
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
//     with run-check.sh in its image (credentials from release.build.credentials, allow-listed like a
//     check's), each of release.build.files stashed (buildReleaseFiles);
//   - release.windows: the workflow dispatched on the tag, its files from GitHub (windowsFiles); after
//     a skip, or when that fails, they are missing;
//   - everything gathered on the release agent, release.validate run over it, and the validated set
//     stashed as release-files (gatherReleaseFiles).
// Returns [stash: 'release-files', missing: the missing Windows files (globs, or the workflow's name),
// reason: why they are missing].
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
        echo "The Windows files are missing (${got.reason}): ${files.missing.join(', ')}. The release goes on without them."
      } else {
        windows = got.stashes
      }
    }
    files.stash = gatherReleaseFiles(ctx, releaseConfig, tag, sha, built, windows)
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
      withCheckCredentials([name: 'release-build', credentials: build.credentials], entry) { String envFile ->
        withEnv(["YGG_IMAGE=${build.image}", "YGG_RUN=${build.run}", "YGG_ENV_FILE=${envFile ?: "${scratch()}/release-build.env"}",
                 "YGG_TAG=${tag}", "YGG_VERSION=${tag.replaceFirst('^v', '')}"]) {
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
    return [stashes: null, reason: "the files of ${workflow} run ${run} could not be had: ${safeText(problem.message, 300)}"]
  }
}

// What a release lists as missing when its Windows files could not be had: release.windows.files, or
// the workflow's name when it declares none.
@NonCPS
List windowsNames(Map windows) {
  return windows.files ? (windows.files as List).collect { it.toString() } : [windows.workflow.toString()]
}

// On the release agent: the release's files in release-files/ (release.build's under build/, at their
// paths in the checkout; the Windows ones under windows/<artifact>/), with no link among them, then
// release.validate run over them -- in its image, from the application's root (the tag checked out), the
// whole workspace mounted and RELEASE_FILES, RELEASE_TAG and RELEASE_VERSION in its environment -- and
// what it leaves there (checked for links again: it ran as root) stashed as release-files: the set the
// uploads are checked against. A validation that fails stops the release here: publish-release.sh, which
// is what creates the drafts, is never called, so there is nothing to publish (and no draft at all).
// Returns the stash's name.
String gatherReleaseFiles(Map ctx, Map releaseConfig, String tag, String sha, List built, List windows) {
  Map validate = releaseConfig.validate as Map
  node('release') {
    try {
      if (validate) {
        checkoutTag(ctx, tag, sha)
      }
      dir('release-files') {
        deleteDir()
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
      }
      if (validate) {
        withEnv(["YGG_IMAGE=${validate.image}", "YGG_RUN=${validate.run}", "YGG_TMP=${scratch()}", "YGG_TAG=${tag}",
                 "YGG_VERSION=${tag.replaceFirst('^v', '')}"]) {
          int code = sh(script: '''
            umask 077
            mkdir -p "$YGG_TMP"
            env_file="$YGG_TMP/release-validate.env"
            {
              printf 'RELEASE_FILES=%s\\n' "$WORKSPACE/release-files"
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
        dir('release-files') {
          refuseLinks(true, 'the validated release files')
        }
      }
      dir('release-files') {
        stash name: 'release-files', includes: '**', allowEmpty: true
      }
    } finally {
      cleanScratch()
      cleanWs()
    }
  }
  return 'release-files'
}

// Stage Publish the release, on the publish agent: the notes (release_notes.py: the version's CHANGELOG.md
// section, else the pull requests merged since the previous tag), the files (the release-files stash,
// nothing else) and publish-release.sh, with the release's GitHub copy when the catalog's mirror.releases
// is on (the tag waited for first, outside node(): the mirror pushes it). Returns the releases' addresses.
List publishRelease(Map ctx, Map entry, String version, String tag, String sha, Map files) {
  boolean mirrorReleases = mirrorsReleases(ctx)
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
      withEnv(["YGG_TMP=${tmp}", "YGG_TAG=${tag}", "YGG_VERSION=${version}", "YGG_TAGS=${entry?.tags ?: ctx.tags}"]) {
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

// One glob's files from the current directory, as the stash `name`: none fails the release (a declared
// file that is not there is a broken release).
void stashGlob(String name, String glob, String field) {
  try {
    stash name: name, includes: glob, allowEmpty: false
  } catch (FlowInterruptedException interruption) {
    throw interruption
  } catch (Exception problem) {
    error "${field}: '${printable(glob)}' matches no file (${safeText(problem.message, 200)})"
  }
}

// What a release section must look like before anything of it is used (pipeline_config.py checks the
// fields; this is the library's own look at what reaches a shell, a stash or GitHub). null when fine.
@NonCPS
String releaseProblem(Map releaseConfig) {
  for (String part : ['build', 'validate']) {
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
  if (windows != null && !(windows.workflow instanceof String && windows.workflow ==~ WORKFLOW)) {
    return "release.windows.workflow: '${printable(windows?.workflow)}' is not a workflow file (name.yml)"
  }
  for (Map files : [[field: 'release.build.files', globs: build?.files], [field: 'release.windows.files', globs: windows?.files]]) {
    for (Object glob : (files.globs ?: []) as List) {
      if (!(glob instanceof String) || !(glob ==~ RELEASE_GLOB) || glob.split('/').any { it in ['.', '..', ''] }) {
        return "${files.field}: '${printable(glob)}' is not a relative glob (letters, digits, . _ * ? + @ = - and /, no . or .. part)"
      }
    }
  }
  return null
}

// ---- The actions-release job ----

// The actions-release job (platform/jenkins/controller/casc.yaml, its script jenkins/actions-release.groovy
// kept in Jenkins): the Windows files of a release that was published without them (after the skip, or
// a Windows run that failed), attached to it. Parameters REPOSITORY (a catalog id whose yggdrasil.yml at
// the tag declares release.windows) and TAG (one of its version tags, which must exist). The workflow is
// dispatched on the tag and waited for outside node(); its files are downloaded on the publish agent and
// put on the release (and its GitHub copy) with publish-release.sh attach. Under the application's release
// lock, so it never runs next to that application's release.
void actionsRelease() {
  String app = (params.REPOSITORY ?: '').toString().trim()
  String tag = (params.TAG ?: '').toString().trim()
  Map ctx = catalogContext(app)
  if (!(tag ==~ TAG) || !globMatches(ctx.tags, tag)) {
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
        Map described = null
        stage('Read the release') {
          node('release') {
            try {
              checkoutTools()
              String tmp = scratch()
              int code = withForgeToken('read') {
                return toolStatus('forge.sh', ['tag-sha', ctx.repo, tag], "${tmp}/tag-sha")
              }
              if (code == 4) {
                error "${app} has no tag ${tag}."
              }
              if (code != 0) {
                error "forge.sh tag-sha exited ${code}; its message is above."
              }
              String sha = readScratch(tmp, 'tag-sha').trim()
              if (!(sha ==~ COMMIT)) {
                error "forge.sh tag-sha answered '${printable(sha)}', not a commit"
              }
              checkoutTag(ctx, tag, sha)
              Map document = describeCheckout(ctx)
              described = [sha: sha, config: document.config]
            } finally {
              cleanScratch()
              cleanWs()
            }
          }
        }
        Map releaseConfig = described.config?.release as Map
        if (!releaseConfig?.windows) {
          error "${app}'s yggdrasil.yml at ${tag} declares no release.windows: there are no Windows files to attach."
        }
        String problem = releaseProblem(releaseConfig)
        if (problem) {
          error "yggdrasil.yml's release cannot be used: ${problem}"
        }
        Map got = null
        stage('Windows files') {
          got = windowsFiles(ctx, releaseConfig.windows as Map, tag, seconds)
        }
        if (got.stashes == null) {
          error "No Windows files for ${tag}: ${got.reason}. Nothing was attached."
        }
        boolean mirrorReleases = mirrorsReleases(ctx)
        stage('Attach') {
          List urls = onPublishAgent(ctx, true, mirrorReleases) {
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
            String out = ''
            withEnv(["GITHUB_OWNER=${githubOwner(ctx)}"]) {
              out = tool('publish-release.sh', arguments, true)
            }
            return (out ?: '').readLines().findAll { it.trim() }
          }
          echo "Attached the Windows files of ${tag}: ${urls.join(' ')}"
        }
        currentBuild.description = "${app} ${tag}: Windows files attached"
      }
    }
  }
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

// This repository (its main branch, from the same git server) in ./yggdrasil, with the read credential.
void checkoutTools() {
  dir('yggdrasil') {
    git url: "${env.FORGE_URL}/${env.GITHUB_OWNER}/${env.YGGDRASIL_REPOSITORY}.git", branch: 'main',
        credentialsId: forgeCredential('read')
  }
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

// Whether the plan covers the file's checks exactly: every check of yggdrasil.yml once, and nothing
// else. A check the plan left out would never run and never report, and a required status that never
// comes only blocks the merge -- on a push it would deploy unchecked. null when fine.
@NonCPS
String planCoverage(List planned, Map config) {
  List names = planned.collect { it.name }
  List wanted = new ArrayList(((config.checks ?: [:]) as Map).keySet())
  List missing = wanted.findAll { !(it in names) }
  if (missing) {
    return "the plan leaves out ${missing.collect { printable(it) }.join(', ')}"
  }
  List extra = names.findAll { !(it in wanted) }
  if (extra) {
    return "the plan has checks yggdrasil.yml does not: ${extra.collect { printable(it) }.join(', ')}"
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
