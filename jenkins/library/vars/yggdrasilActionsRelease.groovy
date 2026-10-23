// The actions-release job's entry point. Its script, kept in Jenkins (jenkins/actions-release.groovy,
// loaded by the Job DSL in platform/jenkins/controller/casc.yaml), is only
//
//     @Library('yggdrasil') _
//     yggdrasilActionsRelease()
//
// so what the job does runs here, in the trusted library, not in the sandboxed script: it attaches the
// Windows files of a release that was published without them (after the skip, or a Windows run that
// failed). The parameters REPOSITORY (a catalog id) and TAG (one of its release tags) are checked in
// yggdrasilPipeline.actionsRelease(), which shares the release files' steps with the release pull
// request: one copy of that logic.
def call() {
  yggdrasilPipeline.actionsRelease()
}
