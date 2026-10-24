// The release-resume job's script, kept in Jenkins like the default Jenkinsfile: the Job DSL in
// platform/jenkins/controller/casc.yaml reads it from /etc/jenkins/release-resume.groovy. It finishes the
// release of a tag that exists already (a release that failed after its merge, the GitHub copy after the
// skip, a library's packages); the owner runs it with "Build with Parameters" (REPOSITORY, TAG).
// Everything it does is in the pinned library (yggdrasilReleaseResume, then yggdrasilPipeline.releaseResume).
@Library('yggdrasil') _
yggdrasilReleaseResume()
