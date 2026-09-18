using System.Net;
using System.Net.Http.Headers;
using System.Text.Json.Nodes;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.AspNetCore.TestHost;
using Microsoft.Extensions.DependencyInjection;
using Yggdrasil.Status.Api;
using Yggdrasil.Status.Docker;
using Yggdrasil.Status.Model;
using Yggdrasil.Status.Probing;

namespace Yggdrasil.Status.Tests;

/// <summary>
/// The whole service in memory, with Docker and the applications' health endpoints replaced by fake
/// HTTP handlers, so the real clients, refresher, JSON and pipeline all run.
/// </summary>
public sealed class StatusApiFactory : WebApplicationFactory<Program>
{
    public const string AllowedOrigin = "https://yggdrasil.hml.example.com";

    public FakeDocker Docker { get; } = new FakeDocker()
        .Add(new FakeContainer("api1", "heimdall-api", Health: "healthy", ExtraLabels: new()
        {
            ["yggdrasil.version"] = "1.4.0",
            ["yggdrasil.commit"] = "3f2a9c1",
            ["yggdrasil.deployed_at"] = "2026-09-17T21:40:02Z",
        }))
        .Add(new FakeContainer("ui1", "heimdall-ui", Image: "heimdall-ui:1.4.0-3f2a9c1"))
        .Add(new FakeContainer("traefik1", "yggdrasil", Service: "traefik", Image: "traefik:v3.5"));

    public FakeProbes Probes { get; } = new FakeProbes()
        .Answer("heimdall-api", HttpStatusCode.OK)
        .Answer("heimdall-ui", HttpStatusCode.OK)
        .Answer("traefik", HttpStatusCode.OK);

    /// <summary>Pretend every request arrived on the internal port (TestServer reports port 0).</summary>
    public bool OnInternalPort { get; init; }

    /// <summary>YGGDRASIL_ENVIRONMENT; the test catalog has development, homologation and production.</summary>
    public string Environment { get; init; } = "production";

    protected override void ConfigureWebHost(IWebHostBuilder builder)
    {
        builder.UseSetting("YGGDRASIL_STATUS_TOKEN", TestData.Token);
        builder.UseSetting("YGGDRASIL_ENVIRONMENT", Environment);
        builder.UseSetting("YGGDRASIL_DOMAIN", "example.com");
        builder.UseSetting("YGGDRASIL_STATUS_CORS_ORIGINS", AllowedOrigin);

        builder.ConfigureTestServices(services =>
        {
            services.AddSingleton(TestData.Catalog());
            services.AddHttpClient<DockerClient>().ConfigurePrimaryHttpMessageHandler(() => Docker);
            services.AddHttpClient<HealthProber>().ConfigurePrimaryHttpMessageHandler(() => Probes);
            if (OnInternalPort)
            {
                services.AddSingleton<InternalPortPolicy, AlwaysInternal>();
            }
        });
    }

    public async Task<HttpClient> ReadyClientAsync()
    {
        var store = Services.GetRequiredService<StatusSnapshotStore>();
        var deadline = DateTime.UtcNow.AddSeconds(10);
        while (store.Current is null && DateTime.UtcNow < deadline)
        {
            await Task.Delay(20);
        }

        Assert.NotNull(store.Current);
        return CreateClient();
    }

    private sealed class AlwaysInternal(StatusOptions options) : InternalPortPolicy(options)
    {
        public override bool IsInternal(HttpContext context) => true;
    }
}

public class ApiTests : IClassFixture<StatusApiFactory>
{
    // The example of GET /api/status in docs/status-api.md, verbatim: the response must use exactly
    // these property names at every level.
    private const string ContractExample = """
        {
          "environment": "production",
          "environmentName": "Production",
          "generatedAt": "2026-09-18T18:04:11Z",
          "status": "degraded",
          "systems": [
            {
              "id": "heimdall",
              "name": "Heimdall",
              "description": "Identity and access management",
              "status": "up",
              "applications": [
                {
                  "id": "heimdall-api",
                  "name": "Heimdall API",
                  "kind": "api",
                  "status": "up",
                  "url": "https://heimdall-api.example.com",
                  "repository": "https://github.com/artur-rios/heimdall-api",
                  "deployment": {
                    "version": "1.4.0",
                    "commit": "3f2a9c1",
                    "deployedAt": "2026-09-17T21:40:02Z",
                    "image": "heimdall-api:1.4.0-3f2a9c1"
                  },
                  "container": {
                    "state": "running",
                    "health": "healthy",
                    "startedAt": "2026-09-17T21:40:05Z",
                    "restartCount": 0
                  },
                  "probe": {
                    "healthy": true,
                    "statusCode": 200,
                    "latencyMs": 12,
                    "checkedAt": "2026-09-18T18:04:09Z",
                    "error": null
                  }
                }
              ]
            }
          ]
        }
        """;

    private readonly StatusApiFactory factory;

    public ApiTests(StatusApiFactory factory) => this.factory = factory;

    [Theory]
    [InlineData("/api/status", null)]
    [InlineData("/api/status", "Bearer wrong-token-wrong-token-wrong-token-1234")]
    [InlineData("/api/status", "Basic " + TestData.Token)]
    [InlineData("/api/systems/heimdall", null)]
    public async Task GivenNoOrAWrongToken_WhenCallingTheApi_Then401WithAnEmptyBody(string path, string? authorization)
    {
        var client = await factory.ReadyClientAsync();
        using var request = new HttpRequestMessage(HttpMethod.Get, path);
        if (authorization is not null)
        {
            request.Headers.TryAddWithoutValidation("Authorization", authorization);
        }

        using var response = await client.SendAsync(request);

        Assert.Equal(HttpStatusCode.Unauthorized, response.StatusCode);
        Assert.Equal("", await response.Content.ReadAsStringAsync());
    }

    [Fact]
    public async Task GivenTheToken_WhenGettingStatus_ThenTheShapeMatchesTheContract()
    {
        var client = await factory.ReadyClientAsync();

        var body = await GetJson(client, "/api/status");

        AssertSameShape(JsonNode.Parse(ContractExample)!, body, "$");

        Assert.Equal("production", (string?)body["environment"]);
        Assert.Equal("Production", (string?)body["environmentName"]);
        var heimdall = body["systems"]![0]!;
        Assert.Equal("up", (string?)heimdall["status"]);
        var api = heimdall["applications"]![0]!;
        Assert.Equal("up", (string?)api["status"]);
        Assert.Equal("api", (string?)api["kind"]);
        Assert.Equal("https://heimdall-api.example.com", (string?)api["url"]);
        Assert.Equal("https://github.com/artur-rios/heimdall-api", (string?)api["repository"]);
        Assert.Equal("1.4.0", (string?)api["deployment"]!["version"]);
        Assert.Equal("2026-09-17T21:40:05Z", (string?)api["container"]!["startedAt"]);
        Assert.Matches(@"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", (string?)body["generatedAt"]);

        // The platform system: traefik up, jenkins not on this host and so neutral.
        var yggdrasil = body["systems"]![1]!;
        Assert.Equal("up", (string?)yggdrasil["status"]);
        var jenkins = yggdrasil["applications"]![1]!;
        Assert.Equal("not_deployed", (string?)jenkins["status"]);
        Assert.Null(jenkins["deployment"]);
        Assert.Null(jenkins["repository"]);
        Assert.Equal("https://jenkins.example.com", (string?)jenkins["url"]);
        // Written as null, not left out.
        Assert.True(jenkins.AsObject().ContainsKey("container"));
        Assert.True(jenkins.AsObject().ContainsKey("probe"));

        Assert.Equal("up", (string?)body["status"]);
    }

    [Fact]
    public async Task GivenAKnownSystem_WhenGettingIt_ThenItIsOneElementOfSystems()
    {
        var client = await factory.ReadyClientAsync();

        var system = await GetJson(client, "/api/systems/heimdall");

        AssertSameShape(JsonNode.Parse(ContractExample)!["systems"]![0]!, system, "$");
        Assert.Equal("heimdall", (string?)system["id"]);
    }

    [Fact]
    public async Task GivenAnUnknownSystem_WhenGettingIt_Then404()
    {
        var client = await factory.ReadyClientAsync();

        using var response = await client.SendAsync(Authorized("/api/systems/nope"));

        Assert.Equal(HttpStatusCode.NotFound, response.StatusCode);
    }

    [Fact]
    public async Task GivenApplicationsNotInTheEnvironment_WhenGettingStatus_ThenTheyAndTheSystemsTheyEmptyAreLeftOut()
    {
        var client = await factory.ReadyClientAsync();

        var body = await GetJson(client, "/api/status");

        // heimdall-worker and sandbox (whose only application is sandbox-api) are development-only.
        Assert.Equal(["heimdall", "yggdrasil"], body["systems"]!.AsArray().Select(s => (string?)s!["id"]));
        Assert.Equal(["heimdall-api", "heimdall-ui"], body["systems"]![0]!["applications"]!.AsArray().Select(a => (string?)a!["id"]));

        using var sandbox = await client.SendAsync(Authorized("/api/systems/sandbox"));
        Assert.Equal(HttpStatusCode.NotFound, sandbox.StatusCode);

        // Never probed either: nothing in this environment is supposed to answer.
        Assert.DoesNotContain(factory.Docker.Requests.ToList(), r => r.Contains("heimdall-worker") || r.Contains("sandbox-api"));
    }

    [Fact]
    public async Task GivenAnotherEnvironment_WhenGettingStatus_ThenItsOwnApplicationsAndNameAreReported()
    {
        await using var development = new StatusApiFactory { Environment = "development" };
        var client = await development.ReadyClientAsync();

        var body = await GetJson(client, "/api/status");
        var system = await GetJson(client, "/api/systems/sandbox");

        Assert.Equal("development", (string?)body["environment"]);
        Assert.Equal("Development", (string?)body["environmentName"]);
        Assert.Equal(["heimdall", "yggdrasil", "sandbox"], body["systems"]!.AsArray().Select(s => (string?)s!["id"]));
        Assert.Contains(body["systems"]![0]!["applications"]!.AsArray(), a => (string?)a!["id"] == "heimdall-worker");
        Assert.Equal("sandbox-api", (string?)system["applications"]![0]!["id"]);
    }

    [Fact]
    public async Task GivenAnEnvironmentNotInTheCatalog_WhenStarting_ThenStartupIsRefusedWithTheReason()
    {
        await using var staging = new StatusApiFactory { Environment = "staging" };
        var stderr = new StringWriter();
        var original = Console.Error;

        // Program's own start-up check catches the error, prints it and returns before any host is
        // started, which is all the factory gets to see.
        Console.SetError(stderr);
        try
        {
            Assert.Throws<InvalidOperationException>(() => staging.CreateClient());
        }
        finally
        {
            Console.SetError(original);
        }

        Assert.Contains("yggdrasil-status: invalid configuration:", stderr.ToString());
        Assert.Contains("YGGDRASIL_ENVIRONMENT 'staging' is not an environment in the catalog (development, homologation, production)",
            stderr.ToString());
    }

    [Fact]
    public async Task GivenNoToken_WhenGettingHealthz_Then200Ok()
    {
        var client = await factory.ReadyClientAsync();

        using var response = await client.GetAsync("/healthz");

        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
        Assert.Equal("ok", await response.Content.ReadAsStringAsync());
    }

    [Fact]
    public async Task GivenThePublicPort_WhenGettingPrometheusTargets_Then404()
    {
        var client = await factory.ReadyClientAsync();

        using var response = await client.GetAsync("/internal/prometheus/targets");

        Assert.Equal(HttpStatusCode.NotFound, response.StatusCode);
    }

    [Fact]
    public void GivenTheLocalPort_WhenCheckingForTheInternalPort_ThenOnlyThatPortIsInternal()
    {
        Assert.True(InternalPortPolicy.IsInternalPort(8081, 8081));
        Assert.False(InternalPortPolicy.IsInternalPort(8080, 8081));
        Assert.False(InternalPortPolicy.IsInternalPort(0, 8081));
    }

    [Fact]
    public async Task GivenTheInternalPort_WhenGettingPrometheusTargets_ThenHttpServiceDiscoveryJsonWithoutAToken()
    {
        await using var onInternal = new StatusApiFactory { OnInternalPort = true };
        var client = onInternal.CreateClient();

        using var response = await client.GetAsync("/internal/prometheus/targets");

        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
        var targets = JsonNode.Parse(await response.Content.ReadAsStringAsync())!.AsArray();
        Assert.Equal(3, targets.Count);
        Assert.Equal("""{"targets":["heimdall-api:9464"],"labels":{"system":"heimdall","app":"heimdall-api","kind":"api"}}""",
            targets[0]!.ToJsonString());
        Assert.Equal("/prometheus/", (string?)targets[2]!["labels"]!["__metrics_path__"]);
        // Not heimdall-worker nor sandbox-api, which have metrics but are not deployed to production.
        Assert.Equal(["heimdall-api", "traefik", "jenkins"], targets.Select(t => (string?)t!["labels"]!["app"]));
    }

    [Fact]
    public async Task GivenAnAllowedOrigin_WhenPreflighting_ThenTheAuthorizationHeaderIsAllowed()
    {
        var client = await factory.ReadyClientAsync();

        using var response = await client.SendAsync(Preflight(StatusApiFactory.AllowedOrigin));

        Assert.Equal(HttpStatusCode.NoContent, response.StatusCode);
        Assert.Equal(StatusApiFactory.AllowedOrigin, response.Headers.GetValues("Access-Control-Allow-Origin").Single());
        Assert.Contains("authorization", response.Headers.GetValues("Access-Control-Allow-Headers").Single(), StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public async Task GivenAnotherOrigin_WhenPreflighting_ThenNothingIsAllowed()
    {
        var client = await factory.ReadyClientAsync();

        using var response = await client.SendAsync(Preflight("https://evil.example.org"));

        Assert.False(response.Headers.Contains("Access-Control-Allow-Origin"));
    }

    [Fact]
    public async Task GivenAnAllowedOrigin_WhenGettingStatus_ThenTheResponseCarriesTheOrigin()
    {
        var client = await factory.ReadyClientAsync();
        using var request = Authorized("/api/status");
        request.Headers.Add("Origin", StatusApiFactory.AllowedOrigin);

        using var response = await client.SendAsync(request);

        Assert.Equal(StatusApiFactory.AllowedOrigin, response.Headers.GetValues("Access-Control-Allow-Origin").Single());
    }

    [Fact]
    public async Task GivenTheFirstRefreshHasNotFinished_WhenGettingStatus_Then503WithRetryAfter()
    {
        await using var starting = new StatusApiFactory();
        starting.Docker.Hold = new TaskCompletionSource();
        var client = starting.CreateClient();

        using var response = await client.SendAsync(Authorized("/api/status"));

        Assert.Equal(HttpStatusCode.ServiceUnavailable, response.StatusCode);
        Assert.Equal("5", response.Headers.GetValues("Retry-After").Single());
    }

    [Fact]
    public async Task GivenDockerIsUnreachable_WhenGettingStatus_ThenApplicationsThatAnswerAreUnknown()
    {
        await using var noDocker = new StatusApiFactory();
        noDocker.Docker.Unreachable = true;
        var client = await noDocker.ReadyClientAsync();

        var body = await GetJson(client, "/api/status");

        var applications = body["systems"]!.AsArray().SelectMany(s => s!["applications"]!.AsArray()).ToDictionary(a => (string)a!["id"]!, a => (string?)a!["status"]);
        Assert.Equal("unknown", applications["heimdall-api"]);
        // Jenkins does not answer its probe (no such host) and Docker can't say it isn't deployed.
        Assert.Equal("down", applications["jenkins"]);
        Assert.Equal("degraded", (string?)body["status"]);
    }

    private static HttpRequestMessage Authorized(string path)
    {
        var request = new HttpRequestMessage(HttpMethod.Get, path);
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", TestData.Token);
        return request;
    }

    private static HttpRequestMessage Preflight(string origin)
    {
        var request = new HttpRequestMessage(HttpMethod.Options, "/api/status");
        request.Headers.Add("Origin", origin);
        request.Headers.Add("Access-Control-Request-Method", "GET");
        request.Headers.Add("Access-Control-Request-Headers", "authorization");
        return request;
    }

    private static async Task<JsonNode> GetJson(HttpClient client, string path)
    {
        using var response = await client.SendAsync(Authorized(path));
        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
        Assert.Equal("application/json", response.Content.Headers.ContentType?.MediaType);
        return JsonNode.Parse(await response.Content.ReadAsStringAsync())!;
    }

    // Same property names at every level, and the same JSON kind (object, array, string, number...)
    // wherever both sides have a value; arrays are compared by their first element.
    private static void AssertSameShape(JsonNode expected, JsonNode actual, string path)
    {
        Assert.True(expected.GetValueKind() == actual.GetValueKind(),
            $"{path}: expected a {expected.GetValueKind()}, got a {actual.GetValueKind()}");

        switch (expected)
        {
            case JsonObject expectedObject:
                var actualObject = actual.AsObject();
                Assert.True(expectedObject.Select(p => p.Key).SequenceEqual(actualObject.Select(p => p.Key)),
                    $"{path}: expected properties [{string.Join(", ", expectedObject.Select(p => p.Key))}], " +
                    $"got [{string.Join(", ", actualObject.Select(p => p.Key))}]");
                foreach (var (name, value) in expectedObject)
                {
                    if (value is not null && actualObject[name] is { } actualValue)
                    {
                        AssertSameShape(value, actualValue, $"{path}.{name}");
                    }
                }

                break;

            case JsonArray expectedArray when expectedArray.Count > 0 && actual.AsArray().Count > 0:
                AssertSameShape(expectedArray[0]!, actual.AsArray()[0]!, $"{path}[0]");
                break;
        }
    }
}
