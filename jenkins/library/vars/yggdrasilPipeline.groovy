// The deployment pipeline of every application repository. Each repository's Jenkinsfile is just
//
//     @Library('yggdrasil') _
//     yggdrasilPipeline(stack: '<repository name>')
//
// and the multibranch job (platform/jenkins/controller/casc.yaml) only discovers release/* branches
// and pull requests. What happens depends on which of the two this build is:
//
//   release/x.y.z (branch)            -> deploy to homologation
//   PR release/x.y.z -> main          -> wait for every GitHub Actions check on the pull request's
//                                        head to pass, deploy that commit to production, set the
//                                        deploy/production status (required by the main ruleset),
//                                        merge the pull request, tag and release vx.y.z, delete the
//                                        release branch
//   any other pull request            -> nothing; it is reported and the build ends green
//
// The actual work is scripts/deploy.sh and scripts/github.sh from this repository, checked out next
// to the application, so a deploy by hand runs the same code.

def call(Map config) {
  String stack = config.stack
  String owner = config.owner ?: 'artur-rios'
  String releasePattern = /^release\/(\d+\.\d+\.\d+)$/

  pipeline {
    agent none

    options {
      // One deploy per repository at a time: a second push to a release branch waits for the first
      // deploy instead of racing it.
      disableConcurrentBuilds()
      skipDefaultCheckout()
      buildDiscarder(logRotator(numToKeepStr: '50'))
      timeout(time: 90, unit: 'MINUTES')
    }

    environment {
      GITHUB_OWNER = "${owner}"
    }

    stages {
      stage('Skip') {
        when {
          not {
            anyOf {
              branch pattern: releasePattern, comparator: 'REGEXP'
              allOf {
                changeRequest target: 'main'
                expression { (env.CHANGE_BRANCH ?: '') ==~ releasePattern }
              }
            }
          }
        }
        steps {
          echo "Nothing to deploy for ${env.BRANCH_NAME}${env.CHANGE_TARGET ? " (-> ${env.CHANGE_TARGET})" : ''}: only release branches and release pull requests into main deploy."
        }
      }

      stage('Homologation') {
        when {
          branch pattern: releasePattern, comparator: 'REGEXP'
        }
        agent { label 'homologation' }
        steps {
          script {
            String version = (env.BRANCH_NAME =~ releasePattern)[0][1]
            yggdrasilCheckout()
            deploy('homologation', stack, "${version}-${env.GIT_COMMIT.take(7)}")
          }
        }
        post {
          always { cleanWs() }
        }
      }

      stage('Production') {
        when {
          allOf {
            changeRequest target: 'main'
            expression { (env.CHANGE_BRANCH ?: '') ==~ releasePattern }
          }
        }
        agent { label 'production' }
        stages {
          stage('Wait for GitHub checks') {
            steps {
              script {
                yggdrasilCheckout()
                github("wait-checks ${stack} ${env.GIT_COMMIT} 3600")
                github("set-status ${stack} ${env.GIT_COMMIT} pending deploy/production 'Deploying to production' ${env.BUILD_URL}")
              }
            }
          }

          stage('Deploy') {
            steps {
              script {
                String version = (env.CHANGE_BRANCH =~ releasePattern)[0][1]
                deploy('production', stack, "${version}-${env.GIT_COMMIT.take(7)}")
                github("set-status ${stack} ${env.GIT_COMMIT} success deploy/production 'Deployed to production' ${env.BUILD_URL}")
              }
            }
          }

          stage('Merge, tag and clean up') {
            steps {
              script {
                String version = (env.CHANGE_BRANCH =~ releasePattern)[0][1]
                String mergeSha = github("merge-pr ${stack} ${env.CHANGE_ID} ${env.GIT_COMMIT} 'release: v${version} (#${env.CHANGE_ID})'", true)
                String url = github("release ${stack} ${version} ${mergeSha}", true)
                github("delete-branch ${stack} ${env.CHANGE_BRANCH}")
                currentBuild.description = "v${version} -> ${url}"
                echo "Released v${version}: ${url}"
              }
            }
          }
        }
        post {
          failure {
            script {
              // The pull request stays open and unmergeable (deploy/production is required).
              catchError(buildResult: 'FAILURE', stageResult: 'FAILURE') {
                github("set-status ${stack} ${env.GIT_COMMIT} failure deploy/production 'Production deploy failed' ${env.BUILD_URL}")
              }
            }
          }
          always { cleanWs() }
        }
      }
    }
  }
}

// The application at the build's commit in ./app, and this repository in ./yggdrasil for its
// scripts and stack files.
void yggdrasilCheckout() {
  dir('app') { checkout scm }
  env.GIT_COMMIT = sh(script: 'git -C app rev-parse HEAD', returnStdout: true).trim()
  dir('yggdrasil') {
    git url: "https://github.com/${env.GITHUB_OWNER}/yggdrasil.git", branch: 'main', credentialsId: 'github-app'
  }
}

void deploy(String environment, String stack, String version) {
  sh "bash yggdrasil/scripts/deploy.sh '${environment}' '${stack}' app '${version}'"
}

// Runs scripts/github.sh with the GitHub App's installation token. The token only ever reaches the
// shell through its environment (GH_TOKEN), never the command line, so it cannot end up in the
// script text Jenkins logs.
String github(String arguments, boolean returnStdout = false) {
  withCredentials([usernamePassword(credentialsId: 'github-app', usernameVariable: 'GH_APP', passwordVariable: 'GH_TOKEN')]) {
    return sh(script: "bash yggdrasil/scripts/github.sh ${arguments}", returnStdout: returnStdout)?.toString()?.trim()
  }
}
