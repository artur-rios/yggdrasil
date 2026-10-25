// The release-resume job's entry point. Its script, kept in Jenkins (jenkins/release-resume.groovy,
// loaded by the Job DSL in platform/jenkins/controller/casc.yaml), is only
//
//     @Library('yggdrasil') _
//     yggdrasilReleaseResume()
//
// so what the job does runs here, in the trusted library, not in the sandboxed script: it releases a tag
// that exists already -- a release that failed after its merge, the GitHub copy of a release published
// after the skip, the packages a tag build did not push. The parameters REPOSITORY (a catalog id that
// releases) and TAG (one of its version tags, on main) are checked in yggdrasilPipeline.releaseResume(),
// which runs the same steps as a release pull request after its merge and as a tag build: one copy of the
// release logic.
def call() {
  yggdrasilPipeline.releaseResume()
}
