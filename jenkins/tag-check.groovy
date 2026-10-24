// The tag-check job's script, kept in Jenkins like the default Jenkinsfile: the Job DSL in
// platform/jenkins/controller/casc.yaml reads it from /etc/jenkins/tag-check.groovy. Every day, and by hand
// ("Build Now"), it checks that each published release's tag still points at the commit it was released
// from; a red build names the tags that moved or were deleted (docs/setup.md, step 12, says how to repair
// them). Everything it does is in the pinned library (yggdrasilTagCheck, then yggdrasilPipeline.tagCheck).
@Library('yggdrasil') _
yggdrasilTagCheck()
