// The deployment pipeline of every application repository. Each repository's Jenkinsfile is just
//
//     @Library('yggdrasil') _
//     yggdrasilPipeline(stack: '<application id>')
//
// What a build deploys, and where, comes from catalog.yaml (mounted on the controller, the same file
// the Job DSL reads): the application's environments, each with its options resolved -- default,
// then the environment's value, then the application's override. docs/catalog.md has the options.
//
//   branch pushed            -> every environment with trigger: branch whose `branches` glob matches
//   "Build with Parameters"  -> DEPLOY_TO: one environment with trigger: manual (on a discovered branch)
//   PR release/x.y.z -> main -> wait for every GitHub Actions check on the pull request's head, then
//                               deploy that commit to each trigger: release environment in catalog
//                               order, setting deploy/<environment> (required on main) after each;
//                               then merge the pull request, tag and release vx.y.z, delete the branch
//   any other pull request   -> nothing
//
// An environment with approval: true waits for someone to click "Deploy" first, without holding an
// agent. Each environment deploys on its own agent (`agent`, default its id), so a stage runs on
// the host it deploys to.
//
// The work itself is scripts/deploy.sh and scripts/github.sh from this repository, checked out next
// to the application, so a deploy by hand runs the same code.

import groovy.transform.Field

@Field static final String CATALOG = '/etc/jenkins/catalog.yaml'
@Field static final String RELEASE_BRANCH = /^release\/(\d+\.\d+\.\d+)$/

def call(Map config) {
  String app = config.stack
  Map catalog = loadCatalog()
  List plan = resolve(catalog, app)
  String owner = catalog.owner
  String repository = catalog.repository ?: 'yggdrasil'

  List manual = plan.findAll { it.trigger == 'manual' }.collect { it.id }
  properties([
    disableConcurrentBuilds(),
    buildDiscarder(logRotator(numToKeepStr: '50')),
    parameters([
      choice(name: 'DEPLOY_TO', choices: [''] + manual,
             description: 'Deploy this branch to an environment with trigger: manual. Empty: only what the branch triggers.'),
    ]),
  ])

  timeout(time: 4, unit: 'HOURS') {
    withEnv(["GITHUB_OWNER=${owner}", "YGGDRASIL_REPOSITORY=${repository}"]) {
      if (env.CHANGE_ID) {
        if (env.CHANGE_TARGET == 'main' && (env.CHANGE_BRANCH ?: '') ==~ RELEASE_BRANCH) {
          release(app, plan.findAll { it.trigger == 'release' })
        } else {
          echo "Nothing to deploy for a pull request from ${env.CHANGE_BRANCH} into ${env.CHANGE_TARGET}."
        }
        return
      }

      String branch = env.BRANCH_NAME
      List targets = params.DEPLOY_TO
        ? plan.findAll { it.id == params.DEPLOY_TO && it.trigger == 'manual' }
        : plan.findAll { it.trigger == 'branch' && matchesAny(it.branches, branch) }
      if (!targets) {
        echo "Nothing to deploy for ${branch}: no environment of ${app} is triggered by it (catalog.yaml)."
        return
      }
      String version = (branch ==~ RELEASE_BRANCH) ? (branch =~ RELEASE_BRANCH)[0][1] : branch.replaceAll(/[^A-Za-z0-9_.-]/, '-')
      for (Map environment : targets) {
        deployTo(app, environment, version, null)
      }
    }
  }
}

// The release pull request: every release environment in order, then merge, tag, clean up.
void release(String app, List environments) {
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
        github("wait-checks ${app} ${sha} ${environments[0].checksTimeout}")
        for (Map environment : environments) {
          github("set-status ${app} ${sha} pending deploy/${environment.id} 'Waiting to deploy to ${environment.name}' ${env.BUILD_URL}")
        }
      } finally {
        cleanWs()
      }
    }
  }

  for (Map environment : environments) {
    deployTo(app, environment, version, sha)
  }

  stage('Merge, tag and clean up') {
    node(environments[-1].agent) {
      try {
        checkoutAll()
        String mergeSha = github("merge-pr ${app} ${env.CHANGE_ID} ${sha} 'release: v${version} (#${env.CHANGE_ID})'", true)
        String url = github("release ${app} ${version} ${mergeSha}", true)
        github("delete-branch ${app} ${env.CHANGE_BRANCH}")
        currentBuild.description = "v${version} -> ${url}"
        echo "Released v${version}: ${url}"
      } finally {
        cleanWs()
      }
    }
  }
}

// One environment: wait for approval if it asks for it, then deploy on its agent. With a status
// commit (release pull requests), deploy/<environment> reports the outcome on that commit.
void deployTo(String app, Map environment, String version, String statusSha) {
  stage("Deploy to ${environment.name}") {
    if (environment.approval) {
      // Outside node(): waiting for a person must not hold an agent.
      input message: "Deploy ${app} ${version} to ${environment.name}?", ok: 'Deploy'
    }
    node(environment.agent) {
      try {
        String sha = checkoutAll()
        if (statusSha && sha != statusSha) {
          error "The pull request moved from ${statusSha.take(7)} to ${sha.take(7)} during the release; start it again."
        }
        sh "bash yggdrasil/scripts/deploy.sh '${environment.id}' '${app}' app '${version}-${sha.take(7)}'"
        if (statusSha) {
          github("set-status ${app} ${sha} success deploy/${environment.id} 'Deployed to ${environment.name}' ${env.BUILD_URL}")
        }
      } catch (failure) {
        if (statusSha) {
          catchError(buildResult: 'FAILURE', stageResult: 'FAILURE') {
            github("set-status ${app} ${statusSha} failure deploy/${environment.id} 'Deploy to ${environment.name} failed' ${env.BUILD_URL}")
          }
        }
        throw failure
      } finally {
        cleanWs()
      }
    }
  }
}

// The application at the build's commit in ./app, and this repository in ./yggdrasil for its
// scripts, stack files and catalog. Returns the application commit.
String checkoutAll() {
  dir('app') { checkout scm }
  dir('yggdrasil') {
    git url: "https://github.com/${env.GITHUB_OWNER}/${env.YGGDRASIL_REPOSITORY}.git", branch: 'main', credentialsId: 'github-app'
  }
  return sh(script: 'git -C app rev-parse HEAD', returnStdout: true).trim()
}

// Runs scripts/github.sh with the GitHub App's installation token. The token only ever reaches the
// shell through its environment (GH_TOKEN), never the command line, so it cannot end up in the
// script text Jenkins logs.
String github(String arguments, boolean returnStdout = false) {
  withCredentials([usernamePassword(credentialsId: 'github-app', usernameVariable: 'GH_APP', passwordVariable: 'GH_TOKEN')]) {
    return sh(script: "bash yggdrasil/scripts/github.sh ${arguments}", returnStdout: returnStdout)?.toString()?.trim()
  }
}

// ---- The catalog. Mirrors scripts/catalog.py (DEFAULTS and resolve): keep the two in step. ----

@NonCPS
Map loadCatalog() {
  return new org.yaml.snakeyaml.Yaml().load(new File(CATALOG).getText('UTF-8')) as Map
}

@NonCPS
List resolve(Map catalog, String app) {
  Map defaults = [mode: 'proxy', trigger: 'manual', branches: [], agent: null, approval: false,
                  waitTimeout: 300, keepImages: 3, checksTimeout: 3600]
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
