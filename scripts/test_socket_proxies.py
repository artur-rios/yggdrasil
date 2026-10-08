"""Tests for the Docker socket proxies of platform/compose.yml.

    python3 -m unittest discover -s scripts

The Docker socket is root on the host. Traefik, Alloy and the status API each reach it through their
own wollomatic/socket-proxy, which lets a request through only when its method has an -allow<METHOD>
pattern that matches the whole path. These tests read the proxies' allowlists from compose.yml and
check each one against the requests its client really makes (taken from the clients' sources) and
against requests that must never pass: above all the container inspect for the status API, which
carries every container's environment, i.e. every application's secrets.

With YGG_DOCKER_TESTS=1 (and Docker), ProxyContainerTests also runs the pinned proxy image itself,
then Traefik and Alloy through it, all against a fake Docker API on a socket of their own. Those
tests create one internal network and a few containers named ygg-sp-test-*, and remove them; they
never mount the real Docker socket and never touch another container.
"""

import json
import os
import pathlib
import re
import shutil
import socketserver
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler

try:
    import yaml
except ImportError:  # CI installs it; the static tests need it.
    yaml = None

ROOT = pathlib.Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "platform" / "compose.yml"
ALLOY_CONFIG = ROOT / "platform" / "alloy" / "config.alloy"

# proxy service -> the one service allowed to use it.
PROXIES = {
    "docker-proxy": "status",
    "traefik-docker-proxy": "traefik",
    "alloy-docker-proxy": "alloy",
}

ID = "4f0d1c2b3a4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8"

# What each client sends. The Go clients negotiate the API version with HEAD /_ping (GET when HEAD
# fails), then prefix every path with it.
REQUIRED = {
    # status/src/Yggdrasil.Status/Docker/DockerClient.cs: the list, unversioned, nothing else.
    "status": [
        ("GET", "/containers/json"),
        ("GET", "/v1.51/containers/json"),
    ],
    # traefik v3.6 pkg/provider/docker: pdocker.go (ServerVersion, ContainerList, Events),
    # shared.go inspectContainers and config.go (the container of a network_mode: container:...).
    "traefik": [
        ("HEAD", "/_ping"),
        ("GET", "/_ping"),
        ("GET", "/v1.51/version"),
        ("GET", "/v1.51/containers/json"),
        ("GET", f"/v1.51/containers/{ID}/json"),
        ("GET", "/v1.51/containers/yggdrasil-traefik-1/json"),
        ("GET", "/v1.51/events"),
    ],
    # alloy v1.10 discovery.docker (prometheus discovery/moby: ContainerList, NetworkList) and
    # loki.source.docker (dockertarget: ContainerInspect, ContainerLogs; runner: ContainerInspect).
    "alloy": [
        ("HEAD", "/_ping"),
        ("GET", "/_ping"),
        ("GET", "/v1.49/containers/json"),
        ("GET", "/v1.49/networks"),
        ("GET", f"/v1.49/containers/{ID}/json"),
        ("GET", f"/v1.49/containers/{ID}/logs"),
    ],
}

# Never, for any client: anything that writes (create, start, exec, remove, build, pull...), the
# endpoints that read a container's files or attach to it, and Swarm secrets.
FORBIDDEN_FOR_ALL = [
    ("POST", "/v1.51/containers/create"),
    ("POST", f"/v1.51/containers/{ID}/start"),
    ("POST", f"/v1.51/containers/{ID}/exec"),
    ("POST", "/v1.51/exec/abc/start"),
    ("DELETE", f"/v1.51/containers/{ID}"),
    ("PUT", f"/v1.51/containers/{ID}/archive"),
    ("POST", "/v1.51/build"),
    ("POST", "/v1.51/images/create"),
    ("POST", "/v1.51/volumes/create"),
    ("GET", f"/v1.51/containers/{ID}/archive"),
    ("HEAD", f"/v1.51/containers/{ID}/archive"),
    ("GET", f"/v1.51/containers/{ID}/export"),
    ("GET", f"/v1.51/containers/{ID}/attach/ws"),
    ("GET", f"/v1.51/containers/{ID}/top"),
    ("GET", f"/v1.51/containers/{ID}/changes"),
    ("GET", "/v1.51/exec/abc/json"),
    ("GET", "/v1.51/secrets"),
    ("GET", "/v1.51/configs"),
    ("GET", "/v1.51/info"),
    ("GET", "/v1.51/images/json"),
    ("GET", "/v1.51/volumes"),
    ("GET", "/v1.51/containers/json/../abc/archive"),
    ("GET", "/v1.51/containers/json/extra"),
    ("GET", "/v2.0/containers/json/../../containers/abc/archive"),
    ("GET", "/x/v1.51/containers/json"),
    ("GET", "/containers/json\n"),
]

FORBIDDEN = {
    # Above all the inspect: Config.Env is every application's secrets.
    "status": FORBIDDEN_FOR_ALL + [
        ("GET", f"/containers/{ID}/json"),
        ("GET", f"/v1.51/containers/{ID}/json"),
        ("GET", "/v1.51/containers/yggdrasil-traefik-1/json"),
        ("GET", f"/v1.51/containers/{ID}/logs"),
        ("GET", "/v1.51/events"),
        ("GET", "/v1.51/version"),
        ("GET", "/_ping"),
        ("HEAD", "/_ping"),
        ("HEAD", "/containers/json"),
        ("GET", "/v1.51/networks"),
    ],
    "traefik": FORBIDDEN_FOR_ALL + [
        ("GET", f"/v1.51/containers/{ID}/logs"),
        ("GET", "/v1.51/networks"),
        ("HEAD", "/v1.51/version"),
    ],
    "alloy": FORBIDDEN_FOR_ALL + [
        ("GET", "/v1.51/events"),
        ("GET", "/v1.51/version"),
        ("GET", f"/v1.51/containers/{ID}/stats"),
    ],
}


def load_compose():
    return yaml.safe_load(COMPOSE.read_text())


def allowlist(service):
    """The proxy's -allowfrom and its {METHOD: [pattern, ...]} from the service's command."""
    allowed, allow_from = {}, None
    for argument in service["command"]:
        name, _, value = argument.lstrip("-").partition("=")
        if name == "allowfrom":
            allow_from = value
        elif name.startswith("allow") and name[5:].isupper():
            allowed.setdefault(name[5:], []).append(value)
    return allow_from, allowed


def passes(allowed, method, path):
    """socket-proxy's own check: regexp.Compile("^" + pattern + "$") against the request path. Go's $
    is the end of the text (Python's \\Z), and this compiles the pattern exactly as the proxy does, so
    a top-level | in a pattern would leak here as it would there."""
    return any(re.search("^" + pattern + r"\Z", path) for pattern in allowed.get(method, []))


def top_level_alternation(pattern):
    depth, escaped = 0, False
    for character in pattern:
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character in "([":
            depth += 1
        elif character in ")]":
            depth -= 1
        elif character == "|" and depth == 0:
            return True
    return False


@unittest.skipIf(yaml is None, "needs PyYAML (pip install pyyaml)")
class AllowlistTests(unittest.TestCase):
    def setUp(self):
        self.compose = load_compose()
        self.services = self.compose["services"]

    def test_given_each_proxy_when_its_client_calls_docker_then_every_request_it_makes_passes(self):
        for proxy, client in PROXIES.items():
            _, allowed = allowlist(self.services[proxy])
            for method, path in REQUIRED[client]:
                with self.subTest(proxy=proxy, request=f"{method} {path}"):
                    self.assertTrue(passes(allowed, method, path))

    def test_given_each_proxy_when_asked_for_anything_else_then_it_refuses(self):
        for proxy, client in PROXIES.items():
            _, allowed = allowlist(self.services[proxy])
            for method, path in FORBIDDEN[client]:
                with self.subTest(proxy=proxy, request=f"{method} {path}"):
                    self.assertFalse(passes(allowed, method, path))

    def test_given_the_status_proxy_then_the_container_list_is_all_it_allows(self):
        allow_from, allowed = allowlist(self.services["docker-proxy"])
        self.assertEqual(allowed, {"GET": [r"(/v1\.[0-9]+)?/containers/json"]})
        self.assertEqual(allow_from, "status")

    def test_given_each_proxy_then_only_reads_are_allowed_and_only_from_its_client(self):
        for proxy, client in PROXIES.items():
            with self.subTest(proxy=proxy):
                allow_from, allowed = allowlist(self.services[proxy])
                self.assertEqual(allow_from, client)
                self.assertLessEqual(set(allowed), {"GET", "HEAD"})
                # Nothing else in the command: no -proxycontainername (label allowlists), no
                # -allowbindmountfrom, no second -listenip.
                for argument in self.services[proxy]["command"]:
                    self.assertRegex(argument, r"^-(allowfrom|allowGET|allowHEAD)=")

    def test_given_each_pattern_then_the_proxys_own_anchors_hold(self):
        # The proxy writes "^" + pattern + "$": a top-level | would leave one side unanchored, and
        # Compose would interpolate a $.
        for proxy in PROXIES:
            _, allowed = allowlist(self.services[proxy])
            for pattern in (p for patterns in allowed.values() for p in patterns):
                with self.subTest(proxy=proxy, pattern=pattern):
                    self.assertFalse(top_level_alternation(pattern))
                    self.assertNotIn("$", pattern)
                    self.assertFalse(pattern.startswith("^"))

    def test_given_each_proxy_then_it_is_the_pinned_image_hardened_and_on_a_network_of_its_own(self):
        images = set()
        for proxy, client in PROXIES.items():
            with self.subTest(proxy=proxy):
                service = self.services[proxy]
                images.add(service["image"])
                self.assertRegex(service["image"], r"^wollomatic/socket-proxy:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}$")
                self.assertTrue(service["read_only"])
                self.assertEqual(service["cap_drop"], ["ALL"])
                self.assertIn("no-new-privileges:true", service["security_opt"])
                self.assertEqual(service["volumes"], ["/var/run/docker.sock:/var/run/docker.sock:ro"])
                self.assertNotIn("ports", service)
                [network] = service["networks"]
                self.assertTrue(self.compose["networks"][network]["internal"])
                on_network = sorted(name for name, other in self.services.items() if network in (other.get("networks") or []))
                self.assertEqual(on_network, sorted([proxy, client]))
        self.assertEqual(len(images), 1)

    def test_given_the_platform_then_only_the_proxies_and_the_agent_mount_the_socket(self):
        # The agent deploys (docker compose up), so it needs the whole API; it faces no network
        # but its outgoing WebSocket to the Jenkins controller.
        mounting = sorted(name for name, service in self.services.items()
                          if any("docker.sock" in str(volume) for volume in service.get("volumes") or []))
        self.assertEqual(mounting, sorted([*PROXIES, "agent"]))

    def test_given_each_client_then_it_talks_to_its_own_proxy(self):
        self.assertEqual(self.services["status"]["environment"]["YGGDRASIL_DOCKER_URL"], "http://docker-proxy:2375")
        self.assertIn("--providers.docker.endpoint=tcp://traefik-docker-proxy:2375", self.services["traefik"]["command"])
        alloy = ALLOY_CONFIG.read_text()
        self.assertNotIn("unix://", alloy)
        self.assertEqual(re.findall(r'host\s*=\s*"([^"]+)"', alloy), ["tcp://alloy-docker-proxy:2375"] * 2)


class FakeDocker(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """Just enough of the Docker Engine API, on a Unix socket, for Traefik's provider and Alloy's
    discovery and log tailing to run their normal course. Records every request that reaches it."""

    daemon_threads = True

    def __init__(self, path):
        self.requests = []
        self.stopping = threading.Event()
        super().__init__(str(path), FakeDockerHandler)


CONTAINER_LABELS = {
    "com.docker.compose.project": "whoami",
    "com.docker.compose.service": "whoami",
    "traefik.enable": "true",
    "traefik.http.routers.whoami.rule": "Host(`whoami.example.com`)",
    "traefik.http.services.whoami.loadbalancer.server.port": "8080",
}
NETWORKS = {"edge": {"NetworkID": "n1", "EndpointID": "e1", "IPAddress": "172.30.0.5", "Gateway": "172.30.0.1"}}


class FakeDockerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_HEAD(self):
        self.answer(head=True)

    def do_GET(self):
        self.answer(head=False)

    def answer(self, head):
        path = self.path.split("?", 1)[0]
        self.server.requests.append((self.command, path))
        route = re.sub(r"^/v1\.[0-9]+", "", path)
        if route == "/_ping":
            return self.send(200, b"OK", "text/plain", head, {"Api-Version": "1.51", "OSType": "linux"})
        if route == "/version":
            return self.json({"Version": "28.0.0", "ApiVersion": "1.51", "MinAPIVersion": "1.24", "Os": "linux", "Arch": "amd64"}, head)
        if route == "/containers/json":
            return self.json([{
                "Id": ID, "Names": ["/whoami-whoami-1"], "Image": "whoami:1", "State": "running",
                "Status": "Up 5 minutes", "Created": int(time.time()) - 300, "Labels": CONTAINER_LABELS,
                "HostConfig": {"NetworkMode": "edge"}, "NetworkSettings": {"Networks": NETWORKS},
                "Ports": [], "Mounts": [],
            }], head)
        if route == "/networks":
            return self.json([{"Name": "edge", "Id": "n1", "Driver": "bridge", "Scope": "local", "Labels": {}}], head)
        if route == "/events":
            return self.stream(b"")
        match = re.fullmatch(r"/containers/([^/]+)/(json|logs)", route)
        if match and match.group(1) in (ID, "whoami-whoami-1"):
            if match.group(2) == "logs":
                line = time.strftime("%Y-%m-%dT%H:%M:%S.000000000Z", time.gmtime()).encode() + b" hello\n"
                return self.stream(bytes([1, 0, 0, 0]) + len(line).to_bytes(4, "big") + line)
            return self.json({
                "Id": ID, "Name": "/whoami-whoami-1", "Image": "sha256:abc", "RestartCount": 0,
                "State": {"Status": "running", "Running": True, "StartedAt": "2026-01-01T00:00:00Z",
                          "FinishedAt": "0001-01-01T00:00:00Z"},
                "Config": {"Image": "whoami:1", "Labels": CONTAINER_LABELS, "Tty": False,
                           "Env": ["DATABASE_PASSWORD=never-leave-the-host"]},
                "HostConfig": {"NetworkMode": "edge"},
                "NetworkSettings": {"Networks": NETWORKS, "Ports": {}},
            }, head)
        return self.json({"message": f"page not found: {path}"}, head, status=404)

    def json(self, document, head, status=200):
        self.send(status, json.dumps(document).encode(), "application/json", head)

    def send(self, status, body, content_type, head, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def stream(self, first_chunk):
        # Chunked and never finished, like Docker's event and log streams.
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            if first_chunk:
                self.wfile.write(f"{len(first_chunk):x}\r\n".encode() + first_chunk + b"\r\n")
            self.wfile.flush()
            self.server.stopping.wait()
        except OSError:
            pass
        self.close_connection = True


def docker(*arguments, check=True):
    return subprocess.run(["docker", *arguments], capture_output=True, text=True, check=check, timeout=120)


@unittest.skipUnless(os.environ.get("YGG_DOCKER_TESTS") == "1" and shutil.which("docker") and yaml is not None,
                     "set YGG_DOCKER_TESTS=1 to run the proxies' containers (needs Docker and PyYAML)")
class ProxyContainerTests(unittest.TestCase):
    """The pinned proxy image with compose.yml's own arguments, each proxy in front of a fake Docker
    API of its own, so what reached Docker is known per proxy."""

    CURL = "curlimages/curl:8.22.0@sha256:58adaa4e8dca9c988bae2aba4ab3434a0bb2da16bbe3f92dec39ec7785166777"

    @classmethod
    def setUpClass(cls):
        cls.prefix = f"ygg-sp-test-{uuid.uuid4().hex[:8]}"
        cls.containers = []
        cls.temp = pathlib.Path(tempfile.mkdtemp(prefix="socket-proxy-test-"))
        cls.temp.chmod(0o755)
        cls.fakes = {}
        for proxy in PROXIES:
            fake = cls.fakes[proxy] = FakeDocker(cls.temp / f"{proxy}.sock")
            (cls.temp / f"{proxy}.sock").chmod(0o660)
            threading.Thread(target=fake.serve_forever, daemon=True).start()
        cls.network = cls.prefix
        try:
            docker("network", "create", "--internal", cls.network)
            services = cls.services = load_compose()["services"]
            for proxy in PROXIES:
                service = services[proxy]
                socket_path = cls.temp / f"{proxy}.sock"
                environment = [f"--env={name}={value}" for name, value in service["environment"].items()]
                cls.run_container(
                    proxy, proxy, "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges:true",
                    # The socket's group, as DOCKER_GID is on a host.
                    f"--group-add={socket_path.stat().st_gid}",
                    f"--volume={socket_path}:/var/run/docker.sock:ro", *environment,
                    service["image"], *service["command"])
            for client in PROXIES.values():
                cls.run_container(f"curl-{client}", client, cls.CURL, "sleep", "600")
            cls.run_container("curl-stranger", "stranger", cls.CURL, "sleep", "600")
            for proxy, client in PROXIES.items():
                cls.wait_for(lambda: cls.curl(client, proxy, "GET", "/nothing") == "403", f"{proxy} to listen")
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        for fake in cls.fakes.values():
            fake.stopping.set()
        for name in cls.containers:
            docker("rm", "--force", "--volumes", name, check=False)
        docker("network", "rm", cls.network, check=False)
        for fake in cls.fakes.values():
            fake.shutdown()
            fake.server_close()
        shutil.rmtree(cls.temp, ignore_errors=True)

    @classmethod
    def run_container(cls, role, alias, *arguments):
        name = f"{cls.prefix}-{role}"
        cls.containers.append(name)
        docker("run", "--detach", f"--name={name}", f"--network={cls.network}", f"--network-alias={alias}",
               "--memory=256m", *arguments)
        return name

    @classmethod
    def curl(cls, client, proxy, method, path):
        method_arguments = ["--head"] if method == "HEAD" else ["--request", method]
        result = docker("exec", f"{cls.prefix}-curl-{client}", "curl", "--silent", "--path-as-is", "--max-time", "5",
                        "--output", "/dev/null", "--write-out", "%{http_code}", *method_arguments,
                        f"http://{proxy}:2375{path}", check=False)
        return result.stdout.strip()

    @staticmethod
    def wait_for(condition, what, seconds=30):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.5)
        raise AssertionError(f"timed out waiting for {what}")

    def reached(self, proxy, method, path):
        return (method, path) in self.fakes[proxy].requests

    def blocked(self, proxy):
        logs = docker("logs", f"{self.prefix}-{proxy}", check=False)
        return [line for line in (logs.stdout + logs.stderr).splitlines() if "blocked request" in line]

    def start_client(self, role, *arguments):
        """A real client next to its proxy, removed after the test. Returns a function giving the
        requests the proxy blocked since it started."""
        proxy = next(proxy for proxy, client in PROXIES.items() if client == role)
        before = len(self.blocked(proxy))
        name = self.run_container(role, role, *arguments)
        self.addCleanup(docker, "rm", "--force", "--volumes", name, check=False)
        return name, lambda: self.blocked(proxy)[before:]

    def test_given_each_proxy_when_its_client_asks_then_allowed_requests_reach_docker_and_the_rest_do_not(self):
        for proxy, client in PROXIES.items():
            for method, path in REQUIRED[client]:
                if path.endswith("/events") or path.endswith("/logs"):
                    continue  # streams: covered by the Traefik and Alloy tests below
                with self.subTest(proxy=proxy, request=f"{method} {path}"):
                    # The fake's own answer (200, or 404 for a container it doesn't have).
                    self.assertIn(self.curl(client, proxy, method, path), ("200", "404"))
                    self.assertTrue(self.reached(proxy, method, path))
            for method, path in FORBIDDEN[client]:
                if "\n" in path:
                    continue  # not a valid request line
                with self.subTest(proxy=proxy, request=f"{method} {path}"):
                    self.assertIn(self.curl(client, proxy, method, path), ("403", "405"))
                    self.assertFalse(self.reached(proxy, method, path))

    def test_given_a_container_that_is_not_the_client_when_it_asks_then_it_is_refused(self):
        for proxy in PROXIES:
            with self.subTest(proxy=proxy):
                # A path every proxy allows, and that nothing else in these tests asks for.
                self.assertEqual(self.curl("stranger", proxy, "GET", "/v1.52/containers/json"), "403")
                self.assertFalse(self.reached(proxy, "GET", "/v1.52/containers/json"))

    def test_given_traefik_behind_its_proxy_when_it_starts_then_its_docker_provider_gets_everything_it_needs(self):
        fake = self.fakes["traefik-docker-proxy"]
        fake.requests.clear()
        name, blocked = self.start_client(
            "traefik", self.services["traefik"]["image"], "--log.level=DEBUG", "--providers.docker=true",
            "--providers.docker.endpoint=tcp://traefik-docker-proxy:2375",
            "--providers.docker.exposedbydefault=false", "--providers.docker.network=edge")
        self.wait_for(lambda: any(path.endswith("/events") for _, path in fake.requests), "traefik to watch events")
        self.wait_for(lambda: "whoami" in docker("logs", name, check=False).stderr + docker("logs", name, check=False).stdout,
                      "traefik to build the router of the fake container")
        self.assertEqual(blocked(), [])
        paths = {re.sub(r"^/v1\.[0-9]+", "", path) for _, path in fake.requests}
        self.assertLessEqual({"/_ping", "/version", "/containers/json", f"/containers/{ID}/json", "/events"}, paths)

    def test_given_alloy_behind_its_proxy_when_it_starts_then_it_discovers_and_tails_the_containers(self):
        fake = self.fakes["alloy-docker-proxy"]
        fake.requests.clear()
        _, blocked = self.start_client(
            "alloy", "--env=ENVIRONMENT=test", f"--volume={ALLOY_CONFIG}:/etc/alloy/config.alloy:ro",
            "--tmpfs=/var/lib/alloy/data", self.services["alloy"]["image"],
            "run", "--storage.path=/var/lib/alloy/data", "/etc/alloy/config.alloy")
        self.wait_for(lambda: any(path.endswith("/logs") for _, path in fake.requests), "alloy to tail the logs", 60)
        self.assertEqual(blocked(), [])
        paths = {re.sub(r"^/v1\.[0-9]+", "", path) for _, path in fake.requests}
        self.assertLessEqual({"/_ping", "/containers/json", "/networks", f"/containers/{ID}/json", f"/containers/{ID}/logs"}, paths)


if __name__ == "__main__":
    unittest.main()
