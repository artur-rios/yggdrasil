// The actions-release job's script, kept in Jenkins like the default Jenkinsfile: the Job DSL in
// platform/jenkins/controller/casc.yaml reads it from /etc/jenkins/actions-release.groovy. It attaches
// the Windows files of a release published without them (after the skip); the owner runs it with
// "Build with Parameters" (REPOSITORY, TAG). Everything it does is in the pinned library
// (yggdrasilActionsRelease, then yggdrasilPipeline.actionsRelease).
@Library('yggdrasil') _
yggdrasilActionsRelease()
