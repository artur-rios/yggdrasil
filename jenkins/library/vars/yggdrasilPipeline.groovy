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
//                               lock) wait for every GitHub Actions check on the pull request's head,
//                               then deploy that commit to each trigger: release environment in catalog
//                               order, setting deploy/<environment> (required on main) after each;
//                               then merge the pull request and release vx.y.z. Applications with
//                               releaseArtifacts: true use an immutable tag and private draft, and
//                               wait for their artifact workflow to publish it before cleanup
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
//   - the write credential is bound by reportStatus() only, which posts from the controller, outside
//     any node(): neither the ci nor the release agent ever holds it. Checkouts and reads on those
//     agents use the read credential;
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
// Seconds between two looks at GitHub Actions (copyActionsChecks).
@Field static final int ACTIONS_POLL_SECONDS = 60

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
  String app = config.stack ?: env.JOB_NAME.tokenize('/')[0]
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
  boolean releaseArtifacts = application?.releaseArtifacts == true
  String owner = catalog.owner
  String repository = catalog.repository ?: 'yggdrasil'
  Map forge = forgeOf(catalog)
  Map ctx = [app: app, repo: application != null ? (application.repository ?: app) : app, forge: forge,
             mirror: mirrorOf(catalog), checksTimeout: (plan ? plan[0].checksTimeout : 3600) as int]

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
    // forge.sh reads the git server from these (and the token each call binds), so an agent needs no
    // catalog of its own to talk to it.
    withEnv(["GITHUB_OWNER=${owner}", "YGGDRASIL_REPOSITORY=${repository}", "FORGE_TYPE=${forge.type}",
             "FORGE_URL=${forge.url}", "FORGE_API_URL=${forge.api}", "FORGE_OWNER=${owner}"]) {
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
      release(app, plan.findAll { it.trigger == 'release' }, releaseArtifacts, ctx.repo)
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
  // "Build with Parameters" deploys without checks, as it always did.
  if (!deployToParameter && (branch in ['develop', 'main'] || branch.startsWith('release/'))) {
    Map state = prepare(ctx, 'push', targets as boolean)
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
    deployTo(app, environment, version, null, deployToParameter, ctx.repo, inputs)
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
  Map state = [:]
  stage('Read yggdrasil.yml') {
    node('ci') {
      try {
        state.sha = checkoutAll(true)
        String tmp = scratch()
        // The controller's catalog, the one the job was created from, not the checkout's copy.
        dir(tmp) {
          writeFile file: 'catalog.yaml', text: catalogText()
        }
        withEnv(["YGG_TMP=${tmp}", "YGG_JOB=${ctx.app}", "YGG_CATALOG=${tmp}/catalog.yaml"]) {
          int code = sh(script: '''
            python3 yggdrasil/scripts/pipeline_config.py describe "$YGG_JOB" app >"$YGG_TMP/describe.json"
          ''', returnStatus: true)
          String text = readScratch(tmp, 'describe.json')
          if (!text.trim()) {
            error "pipeline_config.py describe failed (exit ${code}); its message is above."
          }
          Map document = parseJson(text) as Map
          if (document.errors) {
            error "yggdrasil.yml cannot be used:\n${bulleted(document.errors as List)}"
          }
          state.entry = document.entry
          state.config = document.config
          if (state.config != null) {
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
  String problem = planProblem(planned, state.entry)
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
          sameCommit(checkoutAll(true), state.sha)
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
        sameCommit(checkoutAll(true), state.sha)
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
void collectResults(Map check) {
  if (!check.junit && !check.archive) {
    return
  }
  try {
    dir('app') {
      if (check.junit) {
        junit testResults: check.junit, allowEmptyResults: true
      }
      if (check.archive) {
        archiveArtifacts artifacts: check.archive, allowEmptyArchive: true
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
// once a minute -- each look on the publish agent (never ci: the GitHub App's token must not sit next to
// the repositories' check code), the wait between looks outside node(), so waiting never holds an
// agent -- until github.sh actions-status says they all passed (0) or one failed (1), or
// checksTimeout runs out. Then each is copied to the forge with the GitHub run as its target. Not
// finished in time (still running, or GitHub not answering: exit 2 or 3): pending, "GitHub Actions
// didn't answer", and the build ends unstable; a rebuild looks again.
void copyActionsChecks(Map ctx, Map state) {
  List names = state.entry.actionsChecks as List
  if (!ctx.forge.publishAgent) {
    error "GitHub Actions results are read on the publish agent, and catalog.yaml has none (set forge.publishAgent, or have an environment with trigger: release)."
  }
  stage('GitHub Actions') {
    for (String name : names) {
      reportStatus(ctx.repo, state.sha, 'pending', name, 'Waiting for GitHub Actions')
    }
    long deadline = System.currentTimeMillis() + ctx.checksTimeout * 1000L
    Map look = actionsLook(ctx, state.sha, names)
    while (!(look.code in [0, 1]) && System.currentTimeMillis() < deadline) {
      echo "GitHub Actions has not finished (actions-status exit ${look.code}); looking again in ${ACTIONS_POLL_SECONDS} s."
      sleep time: ACTIONS_POLL_SECONDS, unit: 'SECONDS'
      look = actionsLook(ctx, state.sha, names)
    }
    boolean finished = look.code in [0, 1]
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

// One look at the actionsChecks on the mirror (owner mirror.github, the same repository name), on the
// publish agent (forge.publishAgent: the agent that already holds the GitHub App for releases) with the
// App's token. Returns [code: actions-status' exit, out: its lines].
Map actionsLook(Map ctx, String sha, List names) {
  Map look = [code: 2, out: '']
  node(ctx.forge.publishAgent) {
    try {
      checkoutTools()
      String tmp = scratch()
      withEnv(["GITHUB_OWNER=${ctx.mirror.github}"]) {
        withForgeToken('github') {
          look.code = toolStatus('github.sh', ['actions-status', ctx.repo, sha] + names, "${tmp}/actions-status")
        }
      }
      look.out = readScratch(tmp, 'actions-status')
    } finally {
      cleanScratch()
      cleanWs()
    }
  }
  return look
}

// ---- Releases (the release pull request; unchanged apart from the statuses' helper) ----

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
        sha = checkoutAll()
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
        checkoutAll()
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
void deployTo(String app, Map environment, String version, String statusSha, boolean start = false,
              String repo = null, List inputs = []) {
  stage("Deploy to ${environment.name}") {
    if (environment.approval) {
      // Outside node(): waiting for a person must not hold an agent.
      input message: "Deploy ${app} ${version} to ${environment.name}?", ok: 'Deploy'
    }
    String sha = null
    def failure = null
    node(environment.agent) {
      try {
        sha = checkoutAll()
        if (statusSha && sha != statusSha) {
          error "The pull request moved from ${statusSha.take(7)} to ${sha.take(7)} during the release; start it again."
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
        reportStatus(repo, sha, 'success', "deploy/${environment.id}", "Deployed to ${environment.name}")
      } else {
        catchError(buildResult: 'FAILURE', stageResult: 'FAILURE') {
          reportStatus(repo, statusSha, 'failure', "deploy/${environment.id}", "Deploy to ${environment.name} failed")
        }
      }
    }
    if (failure != null) {
      throw failure
    }
  }
}

// ---- Checkouts ----

// The application at the build's commit in ./app, and this repository in ./yggdrasil for its
// scripts, stack files and catalog. Returns the application commit. fullHistory (the ci agent): the
// whole history, develop, main and the tags as well (fetchHistory).
String checkoutAll(boolean fullHistory = false) {
  dir('app') {
    checkout scm
  }
  checkoutTools()
  if (fullHistory) {
    fetchHistory()
  }
  String sha = sh(script: 'git -C app rev-parse HEAD', returnStdout: true).trim()
  if (!(sha ==~ COMMIT)) {
    error "The checkout's HEAD is not a commit: ${printable(sha)}"
  }
  return sha
}

// This repository (its main branch, from the same git server) in ./yggdrasil, with the read credential.
void checkoutTools() {
  dir('yggdrasil') {
    git url: "${env.FORGE_URL}/${env.GITHUB_OWNER}/${env.YGGDRASIL_REPOSITORY}.git", branch: 'main',
        credentialsId: forgeCredential('read')
  }
}

// In ./app: the full history (a shallow checkout is deepened), origin/develop and origin/main, and the
// tags -- what branch-policy.sh compares with (on a shallow clone or without the tags it would pass
// what it should fail) and the base of a pull request's diff. The checkout scm did may have fetched
// only the build's own ref, so this fetches explicitly, with the read credential through a credential
// helper reading the environment: the token is never in an argument or the remote's URL. A branch the
// server does not have is only noted (branch-policy.sh then fails, as it should).
void fetchHistory() {
  withForgeToken('read') {
    sh(script: '''
      set +x
      cd app
      # A refused token fails the fetch instead of waiting for someone to type a password.
      export GIT_TERMINAL_PROMPT=0
      helper='!f() { test "$1" = get || exit 0; echo "username=$FORGE_USER"; echo "password=$FORGE_TOKEN"; }; f'
      git_remote() { git -c credential.helper= -c "credential.helper=$helper" "$@"; }
      if [ "$(git rev-parse --is-shallow-repository)" = true ]; then
        git_remote fetch --quiet --unshallow origin
      fi
      for branch in develop main; do
        found=0
        git_remote ls-remote --exit-code --heads origin "$branch" >/dev/null || found=$?
        case "$found" in
          0) git_remote fetch --quiet --no-tags origin "+refs/heads/$branch:refs/remotes/origin/$branch" ;;
          2) echo "The repository has no $branch branch." >&2 ;;
          *) exit "$found" ;;
        esac
      done
      git_remote fetch --quiet --tags origin
    ''')
  }
}

// The checkout of an earlier step and this one must be the same commit (a multibranch build checks out
// one revision every time; this says so loudly if that ever changes).
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

// The Forgejo -> GitHub mirror ([github: <owner>]), or null (catalog.py mirror()).
@NonCPS
Map mirrorOf(Map catalog) {
  return catalog.mirror ? [github: catalog.mirror.github.toString()] : null
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

@NonCPS
boolean matchesAny(List globs, String branch) {
  return globs.any { glob ->
    String regex = glob.collect { c -> c == '*' ? '.*' : c == '?' ? '.' : java.util.regex.Pattern.quote(c) }.join('')
    branch ==~ regex
  }
}
