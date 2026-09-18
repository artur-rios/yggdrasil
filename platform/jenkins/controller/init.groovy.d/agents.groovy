// Creates one permanent inbound (WebSocket) agent per environment agent in catalog.yaml, on every
// controller start, so adding an environment to the catalog is all it takes to get its agent.
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

def jenkins = Jenkins.get()
agents.each { name ->
  if (jenkins.getNode(name) != null) return
  def launcher = new JNLPLauncher()
  launcher.webSocket = true
  def agent = new DumbSlave(name, '/home/jenkins/agent', launcher)
  agent.labelString = name
  agent.numExecutors = 2
  agent.mode = Node.Mode.EXCLUSIVE
  agent.retentionStrategy = new RetentionStrategy.Always()
  jenkins.addNode(agent)
  println "yggdrasil: created agent '${name}'"
}
