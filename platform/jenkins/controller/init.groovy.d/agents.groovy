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

// The agents of the pipeline itself, by name.
def buildAgents = [
  ci: [executors: 4, label: 'ci'],
  release: [executors: 1, label: 'release'],
]
def clashes = agents.findAll { buildAgents.containsKey(it) }
if (clashes) {
  throw new IllegalStateException("yggdrasil: the agent name(s) ${clashes.join(', ')} are reserved for the pipeline's ci and release agents, " +
    "but an environment in catalog.yaml uses them; rename its `agent` (catalog.yaml) and restart")
}

def jenkins = Jenkins.get()

def create = { String name, int executors, String label ->
  if (jenkins.getNode(name) != null) return
  def launcher = new JNLPLauncher()
  launcher.webSocket = true
  def agent = new DumbSlave(name, '/home/jenkins/agent', launcher)
  agent.labelString = label
  agent.numExecutors = executors
  agent.mode = Node.Mode.EXCLUSIVE
  agent.retentionStrategy = new RetentionStrategy.Always()
  jenkins.addNode(agent)
  println "yggdrasil: created agent '${name}'"
}

agents.each { name -> create(name, 2, name) }
buildAgents.each { name, options -> create(name, options.executors, options.label) }
