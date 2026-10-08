using System.Net;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Yggdrasil.Status.Model;

namespace Yggdrasil.Status.Tests;

/// <summary>
/// The Docker Engine API as platform/compose.yml's docker-proxy serves it to the status API: the
/// container list, filtered by Compose labels, and nothing else. Any other path gets the proxy's 403,
/// so a client that reaches for the inspect (and with it every container's environment) fails here
/// the way it would on a host. Containers are added per Compose project.
/// </summary>
public sealed class FakeDocker : HttpMessageHandler
{
    private readonly List<FakeContainer> containers = [];

    public bool Unreachable { get; set; }

    /// <summary>When set, every request waits for it: a refresh that has not completed yet.</summary>
    public TaskCompletionSource? Hold { get; set; }

    /// <summary>Docker's clock, which its "Up 5 minutes" status text is computed against.</summary>
    public TimeProvider Time { get; init; } = TimeProvider.System;

    public List<string> Requests { get; } = [];

    public FakeDocker Add(FakeContainer container)
    {
        containers.Add(container);
        return this;
    }

    protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
    {
        if (Hold is not null)
        {
            await Hold.Task.WaitAsync(cancellationToken);
        }

        return await Answer(request);
    }

    private Task<HttpResponseMessage> Answer(HttpRequestMessage request)
    {
        var uri = request.RequestUri!;
        lock (Requests)
        {
            Requests.Add(uri.PathAndQuery);
        }

        if (Unreachable)
        {
            throw new HttpRequestException(HttpRequestError.ConnectionError, "Connection refused");
        }

        if (request.Method != HttpMethod.Get || uri.AbsolutePath != "/containers/json")
        {
            return Task.FromResult(new HttpResponseMessage(HttpStatusCode.Forbidden));
        }

        var query = System.Web.HttpUtility.ParseQueryString(uri.Query);
        var filters = JsonSerializer.Deserialize<Dictionary<string, List<string>>>(query["filters"]!)!;
        var wanted = filters["label"];
        var now = Time.GetUtcNow();
        var matching = containers.Where(c => wanted.All(label => c.LabelList().Contains(label)))
            .Select(c => c.Summary(now));
        return Json(new JsonArray([.. matching]));
    }

    private static Task<HttpResponseMessage> Json(JsonNode node) =>
        Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = new StringContent(node.ToJsonString(), Encoding.UTF8, "application/json"),
        });
}

/// <summary>
/// One container as GET /containers/json describes it. Created and StartedAt default to the same
/// moment, as for a container Compose created and started and that has not restarted since.
/// </summary>
public sealed record FakeContainer(
    string Id,
    string Project,
    string? Service = null,
    string State = "running",
    // healthy, unhealthy or starting; null for an image without a health check.
    string? Health = null,
    DateTimeOffset? Created = null,
    DateTimeOffset? StartedAt = null,
    string Image = "heimdall-api:1.4.0-3f2a9c1",
    // Docker Engine 29 (API 1.52) added Health to the list; before, it is only in the Status text.
    bool ListsHealth = true,
    Dictionary<string, string>? ExtraLabels = null)
{
    public static readonly DateTimeOffset DefaultStart = new(2026, 9, 17, 21, 40, 5, TimeSpan.Zero);

    public Dictionary<string, string> Labels()
    {
        var labels = new Dictionary<string, string> { ["com.docker.compose.project"] = Project };
        if (Service is not null)
        {
            labels["com.docker.compose.service"] = Service;
        }

        foreach (var (key, value) in ExtraLabels ?? [])
        {
            labels[key] = value;
        }

        return labels;
    }

    public List<string> LabelList() => Labels().Select(l => $"{l.Key}={l.Value}").ToList();

    public JsonNode Summary(DateTimeOffset now)
    {
        var summary = new JsonObject
        {
            ["Id"] = Id,
            ["Names"] = new JsonArray($"/{Project}-{Service ?? "app"}-1"),
            ["Image"] = Image,
            ["ImageID"] = "sha256:abc",
            ["Created"] = (Created ?? DefaultStart).ToUnixTimeSeconds(),
            ["State"] = State,
            ["Status"] = StatusText(now),
            ["Labels"] = new JsonObject([.. Labels().Select(l => KeyValuePair.Create(l.Key, (JsonNode?)l.Value))]),
        };
        if (ListsHealth)
        {
            summary["Health"] = new JsonObject { ["Status"] = Health ?? "none", ["FailingStreak"] = 0 };
        }

        return summary;
    }

    // What `docker ps` shows in its STATUS column, built the way the engine builds it.
    private string StatusText(DateTimeOffset now)
    {
        var up = $"Up {HumanDuration(now - (StartedAt ?? Created ?? DefaultStart))}";
        return State switch
        {
            "running" => up + Health switch
            {
                null => "",
                "starting" => " (health: starting)",
                _ => $" ({Health})",
            },
            "paused" => up + " (Paused)",
            "restarting" => "Restarting (1) 2 seconds ago",
            "exited" => "Exited (0) 5 minutes ago",
            "created" => "Created",
            "dead" => "Dead",
            _ => State,
        };
    }

    /// <summary>A port of go-units' HumanDuration, which the engine uses for "Up ...".</summary>
    public static string HumanDuration(TimeSpan d)
    {
        var seconds = (int)d.TotalSeconds;
        if (seconds < 1)
        {
            return "Less than a second";
        }

        if (seconds == 1)
        {
            return "1 second";
        }

        if (seconds < 60)
        {
            return $"{seconds} seconds";
        }

        var minutes = (int)d.TotalMinutes;
        if (minutes == 1)
        {
            return "About a minute";
        }

        if (minutes < 60)
        {
            return $"{minutes} minutes";
        }

        // Go's math.Round: half away from zero.
        var hours = (int)Math.Round(d.TotalHours, MidpointRounding.AwayFromZero);
        if (hours == 1)
        {
            return "About an hour";
        }

        if (hours < 48)
        {
            return $"{hours} hours";
        }

        if (hours < 24 * 7 * 2)
        {
            return $"{hours / 24} days";
        }

        if (hours < 24 * 30 * 2)
        {
            return $"{hours / 24 / 7} weeks";
        }

        if (hours < 24 * 365 * 2)
        {
            return $"{hours / 24 / 30} months";
        }

        return $"{(int)d.TotalHours / 24 / 365} years";
    }
}

/// <summary>Answers health probes by host name: a status code, a delay, or a connection failure.</summary>
public sealed class FakeProbes : HttpMessageHandler
{
    private readonly Dictionary<string, Func<CancellationToken, Task<HttpResponseMessage>>> hosts = [];

    public FakeProbes Answer(string host, HttpStatusCode status, TimeSpan delay = default)
    {
        hosts[host] = async token =>
        {
            if (delay > TimeSpan.Zero)
            {
                await Task.Delay(delay, token);
            }

            return new HttpResponseMessage(status);
        };
        return this;
    }

    public FakeProbes Refuse(string host)
    {
        hosts[host] = _ => throw new HttpRequestException(HttpRequestError.ConnectionError, "refused",
            new System.Net.Sockets.SocketException((int)System.Net.Sockets.SocketError.ConnectionRefused));
        return this;
    }

    protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken) =>
        hosts.TryGetValue(request.RequestUri!.Host, out var answer)
            ? answer(cancellationToken)
            : throw new HttpRequestException(HttpRequestError.NameResolutionError, "no such host",
                new System.Net.Sockets.SocketException((int)System.Net.Sockets.SocketError.HostNotFound));
}

public sealed class FixedTime(DateTimeOffset now) : TimeProvider
{
    public DateTimeOffset Now { get; set; } = now;

    public override DateTimeOffset GetUtcNow() => Now;
}

public static class TestData
{
    public const string Token = "0123456789abcdef0123456789abcdef-test";

    public static readonly DateTimeOffset Now = new(2026, 9, 18, 18, 4, 11, TimeSpan.Zero);

    /// <summary>The environments of the test host: every one of the catalog's but local.</summary>
    public static readonly string[] HostEnvironments = ["development", "homologation", "production"];

    public static StatusOptions Options(string domain = "example.com") => new()
    {
        Token = Token,
        Environments = HostEnvironments,
        Domain = domain,
        CatalogPath = "catalog.yaml",
        DockerUrl = new Uri("http://docker-proxy:2375"),
        RefreshInterval = TimeSpan.FromSeconds(15),
        InternalPort = 8081,
        CorsOrigins = ["https://yggdrasil.example.com"],
    };

    // The shape of the default catalog.yaml: a local environment on another machine, and three that
    // share one host, two of them on demand. Not the repository's file, which is the installation's
    // own and changes with it.
    public const string CatalogYaml = """
        owner: artur-rios
        environments:
          - id: local
            name: Local
            mode: ports
            trigger: manual
          - id: development
            name: Development
            trigger: branch
            branches: develop
            agent: vps
            hostSuffix: -dev
            onDemand: true
          - id: homologation
            name: Homologation
            mode: proxy
            trigger: branch
            branches: release/*
            agent: vps
            hostSuffix: -hml
            onDemand: true
          - id: production
            name: Production
            mode: proxy
            trigger: release
            agent: vps
            approval: true
        systems:
          - id: heimdall
            name: Heimdall
            description: Identity and access management
            applications:
              - id: heimdall-api
                name: Heimdall API
                kind: api
                health: http://heimdall-api:8080/healthcheck
                metrics: heimdall-api:9464
                host: heimdall-api
                checks: [test, docker]
              # Every environment, with its own suffix in development.
              - id: heimdall-ui
                name: Heimdall web
                kind: web
                health: http://heimdall-ui:8080/healthz
                host: heimdall
                environments:
                  local:
                  development: { hostSuffix: -preview }
                  homologation:
                  production: {}
              # Not in production: left out of everything the production environment reports. Always on
              # in homologation.
              - id: heimdall-worker
                name: Heimdall worker
                kind: worker
                health: http://heimdall-worker:8080/healthz
                metrics: heimdall-worker:9464
                environments:
                  development:
                  homologation: { waitTimeout: 600, onDemand: false }
          - id: yggdrasil
            name: Yggdrasil
            description: Deployment platform
            applications:
              - id: traefik
                name: Traefik
                kind: platform
                health: http://traefik:8082/ping
                metrics: traefik:8082
                container: { project: yggdrasil, service: traefik }
              - id: jenkins
                name: Jenkins
                kind: platform
                health: http://jenkins:8080/login
                metrics: jenkins:8080
                metricsPath: /prometheus/
                host: jenkins
                container: { project: yggdrasil, service: jenkins }
          # Development only, so outside it the whole system is left out.
          - id: sandbox
            name: Sandbox
            description: Experiments
            applications:
              - id: sandbox-api
                name: Sandbox API
                kind: api
                health: http://sandbox-api:8080/healthz
                metrics: sandbox-api:9464
                container: { project: sandbox, service: api }
                environments: { development: {} }
          # Local only: on no environment of the test host.
          - id: scratch
            name: Scratch
            description: Local experiments
            applications:
              - id: scratch-api
                name: Scratch API
                kind: api
                health: http://scratch-api:8080/healthz
                metrics: scratch-api:9464
                environments: { local: {} }
        """;

    public static Catalog Catalog() => CatalogLoader.Parse(CatalogYaml);

    public static HostCatalog OnHost(params string[] environments) =>
        HostCatalog.For(Catalog(), environments.Length == 0 ? HostEnvironments : environments);

    public static ProbeResult Probe(bool healthy = true, long latencyMs = 12, int? statusCode = 200, string? error = null) =>
        new(healthy, statusCode, latencyMs, Now, error);
}
