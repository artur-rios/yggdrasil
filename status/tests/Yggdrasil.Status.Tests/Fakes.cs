using System.Net;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Yggdrasil.Status.Model;

namespace Yggdrasil.Status.Tests;

/// <summary>
/// The Docker Engine API as tecnativa/docker-socket-proxy serves it: the container list filtered by
/// Compose labels, and the inspect of one container. Containers are added per Compose project.
/// </summary>
public sealed class FakeDocker : HttpMessageHandler
{
    private readonly List<FakeContainer> containers = [];

    public bool Unreachable { get; set; }

    /// <summary>When set, every request waits for it: a refresh that has not completed yet.</summary>
    public TaskCompletionSource? Hold { get; set; }

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

        if (uri.AbsolutePath.EndsWith("/containers/json", StringComparison.Ordinal))
        {
            var query = System.Web.HttpUtility.ParseQueryString(uri.Query);
            var filters = JsonSerializer.Deserialize<Dictionary<string, List<string>>>(query["filters"]!)!;
            var wanted = filters["label"];
            var matching = containers.Where(c => wanted.All(label => c.LabelList().Contains(label)))
                .Select(c => c.Summary());
            return Json(new JsonArray([.. matching]));
        }

        var id = uri.AbsolutePath.Split('/')[^2];
        var container = containers.FirstOrDefault(c => c.Id == id);
        return container is null
            ? Task.FromResult(new HttpResponseMessage(HttpStatusCode.NotFound))
            : Json(container.Inspect());
    }

    private static Task<HttpResponseMessage> Json(JsonNode node) =>
        Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = new StringContent(node.ToJsonString(), Encoding.UTF8, "application/json"),
        });
}

public sealed record FakeContainer(
    string Id,
    string Project,
    string? Service = null,
    string State = "running",
    string? Health = null,
    string StartedAt = "2026-09-17T21:40:05.123456789Z",
    int RestartCount = 0,
    long Created = 1_000,
    string Image = "heimdall-api:1.4.0-3f2a9c1",
    Dictionary<string, string>? ExtraLabels = null)
{
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

    public JsonNode Summary() => new JsonObject
    {
        ["Id"] = Id,
        ["Image"] = "sha256:abc",
        ["State"] = State,
        ["Created"] = Created,
        ["Labels"] = LabelsNode(),
    };

    public JsonNode Inspect() => new JsonObject
    {
        ["Id"] = Id,
        ["RestartCount"] = RestartCount,
        ["State"] = new JsonObject
        {
            ["Status"] = State,
            ["StartedAt"] = StartedAt,
            ["Health"] = Health is null ? null : new JsonObject { ["Status"] = Health },
        },
        ["Config"] = new JsonObject { ["Image"] = Image, ["Labels"] = LabelsNode() },
    };

    private JsonObject LabelsNode() => new([.. Labels().Select(l => KeyValuePair.Create(l.Key, (JsonNode?)l.Value))]);
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

    public static StatusOptions Options(string domain = "example.com") => new()
    {
        Token = Token,
        Environment = "production",
        Domain = domain,
        CatalogPath = "catalog.yaml",
        DockerUrl = new Uri("http://docker-proxy:2375"),
        RefreshInterval = TimeSpan.FromSeconds(15),
        InternalPort = 8081,
        CorsOrigins = ["https://yggdrasil.hml.example.com"],
    };

    public const string CatalogYaml = """
        owner: artur-rios
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
              - id: heimdall-ui
                name: Heimdall web
                kind: web
                health: http://heimdall-ui:8080/healthz
                host: heimdall
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
        """;

    public static Catalog Catalog() => CatalogLoader.Parse(CatalogYaml);

    public static ProbeResult Probe(bool healthy = true, long latencyMs = 12, int? statusCode = 200, string? error = null) =>
        new(healthy, statusCode, latencyMs, Now, error);
}
