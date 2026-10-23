// Creates one permanent inbound (WebSocket) agent per distinct environment agent in catalog.yaml, on
// every controller start, so adding an environment to the catalog is all it takes to get its agent.
// An agent is a host: environments that share a host set the same `agent` (agent: vps for
// development, homologation and production on one VPS) and get one agent between them, whose two
// executors deploy to any of them. Deploys of one application never overlap (each job has
// disableConcurrentBuilds), and deploy.sh names everything per environment.
//
// Two more agents are always created, for the pipeline's checks and releases:
//   ci       4 executors, label ci       runs the repositories' checks (pull request builds included)
//   release  1 executor, label release   builds, signs and validates release files, one at a time
// Neither holds a write credential: they check out with the read-only forge-read credential, and
// everything that changes the forge runs on forge.publishAgent. Their containers are started on the
// host by platform/build-agent.yml, one for each of the ci and release users. `ci` and `release` are
// reserved: an environment whose agent is called either would be the same node, so that is an error.
// Their remote root (where Jenkins puts the workspaces) is CI_AGENT_WORKDIR and RELEASE_AGENT_WORKDIR
// from the controller's environment (platform.env; scripts/platform.sh requires both, absolute): each
// must be the YGG_AGENT_WORKDIR of that user's env/agent.env, the directory build-agent.yml mounts at
// the same path in the agent and on the host. scripts/run-check.sh hands a workspace to the host's
// engine by its path, so a workspace anywhere else would be an empty directory in the check's
// container. A ci or release node whose root differs from the setting is replaced on start (same name,
// so the same agent secret); the environment agents keep /home/jenkins/agent.
//
// An environment's agent is its `agent` option, or its id. Environments that only deploy by hand
// (trigger: manual, no `agent` set, and no application overriding either) need no agent. Agents
// that are no longer in the catalog are left alone -- delete them in Manage Jenkins > Nodes.
//
// The secret each host's agent container needs (JENKINS_AGENT_SECRET in its platform.env) is on the
// node's page: Manage Jenkins > Nodes > <agent>.

import hudson.model.Node
import hudson.slaves.DumbSlave
import hudson.slaves.JNLPLauncher
import hudson.slaves.RetentionStrategy
import jenkins.model.Jenkins
import org.yaml.snakeyaml.Yaml

def file = new File('/etc/jenkins/catalog.yaml')
if (!file.exists()) {
  println 'yggdrasil: /etc/jenkins/catalog.yaml is missing; no agents created'
  return
}
def catalog = new Yaml().load(file.getText('UTF-8'))

def applications = catalog.systems.collectMany { it.applications ?: [] }.findAll { it.kind != 'platform' }
def agents = [] as LinkedHashSet
catalog.environments.each { environment ->
  applications.each { app ->
    if (app.environments != null && !app.environments.containsKey(environment.id)) return
    def options = environment + (app.environments?.get(environment.id) ?: [:])
    if (options.trigger in ['branch', 'release'] || options.agent) {
      agents << (options.agent ?: environment.id)
    }
  }
}

// The agents of the pipeline itself, by name, with the setting that names each one's work directory.
def buildAgents = [
  ci: [executors: 4, label: 'ci', workdir: 'CI_AGENT_WORKDIR'],
  release: [executors: 1, label: 'release', workdir: 'RELEASE_AGENT_WORKDIR'],
]
def clashes = agents.findAll { buildAgents.containsKey(it) }
if (clashes) {
  throw new IllegalStateException("yggdrasil: the agent name(s) ${clashes.join(', ')} are reserved for the pipeline's ci and release agents, " +
    "but an environment in catalog.yaml uses them; rename its `agent` (catalog.yaml) and restart")
}
buildAgents.each { name, options ->
  def path = System.getenv(options.workdir) ?: ''
  if (!path.startsWith('/')) {
    throw new IllegalStateException("yggdrasil: ${options.workdir} must be the absolute work directory of the ${name} agent " +
      "(the YGG_AGENT_WORKDIR of the ${name} user's agent.env), not '${path}'; set it in platform.env and restart")
  }
  options.remoteFS = path
}

def jenkins = Jenkins.get()

// keepInStep: a node that exists with another remote root is replaced (the build agents follow their
// setting); otherwise an existing node is left as it is.
def create = { String name, int executors, String label, String remoteFS = '/home/jenkins/agent', boolean keepInStep = false ->
  def existing = jenkins.getNode(name)
  if (existing != null && (!keepInStep || existing.remoteFS == remoteFS)) return
  def launcher = new JNLPLauncher()
  launcher.webSocket = true
  def agent = new DumbSlave(name, remoteFS, launcher)
  agent.labelString = label
  agent.numExecutors = executors
  agent.mode = Node.Mode.EXCLUSIVE
  agent.retentionStrategy = new RetentionStrategy.Always()
  // addNode replaces a node of the same name.
  jenkins.addNode(agent)
  println existing == null ? "yggdrasil: created agent '${name}'" : "yggdrasil: agent '${name}' now works in ${remoteFS}"
}

agents.each { name -> create(name, 2, name) }
buildAgents.each { name, options -> create(name, options.executors, options.label, options.remoteFS, true) }
