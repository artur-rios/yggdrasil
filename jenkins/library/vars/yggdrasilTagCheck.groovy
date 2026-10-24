// The tag-check job's entry point. Its script, kept in Jenkins (jenkins/tag-check.groovy, loaded by the
// Job DSL in platform/jenkins/controller/casc.yaml), is only
//
//     @Library('yggdrasil') _
//     yggdrasilTagCheck()
//
// so what the job does runs here, in the trusted library, not in the sandboxed script: every day (and by
// hand) it checks that each published release's tag is still on the commit it was released from, and
// fails naming the tags that moved or were deleted. Its one parameter, FORGET (optional), names releases
// deleted on purpose, for it to stop checking. What it does is
// yggdrasilPipeline.tagCheck(), on the ci agent with the read credential only.
def call() {
  yggdrasilPipeline.tagCheck()
}
