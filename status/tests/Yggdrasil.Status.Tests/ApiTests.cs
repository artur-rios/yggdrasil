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

    // One host, three environments: production running, development and homologation (on demand)
    // stopped, as `scripts/ygg.sh env stop` leaves them. Jenkins runs on another host.
    public FakeDocker Docker { get; } = new FakeDocker()
        .Add(new FakeContainer("api-prod", "heimdall-api-production", Health: "healthy", Image: "heimdall-api:production-1.4.0-3f2a9c1",
            ExtraLabels: new()
            {
                ["yggdrasil.version"] = "1.4.0",
                ["yggdrasil.commit"] = "3f2a9c1",
                ["yggdrasil.deployed_at"] = "2026-09-17T21:40:02Z",
                ["yggdrasil.environment"] = "production",
            }))
        .Add(new FakeContainer("ui-prod", "heimdall-ui-production", Image: "heimdall-ui:production-1.4.0-3f2a9c1"))
        .Add(new FakeContainer("api-dev", "heimdall-api-development", State: "exited", Image: "heimdall-api:development-1.5.0-8d01e7a"))
        .Add(new FakeContainer("ui-dev", "heimdall-ui-development", State: "exited", Image: "heimdall-ui:development-1.5.0-8d01e7a"))
        .Add(new FakeContainer("worker-dev", "heimdall-worker-development", State: "exited", Image: "heimdall-worker:development-1.5.0-8d01e7a"))
        .Add(new FakeContainer("sandbox-dev", "sandbox-development", Service: "api", State: "exited", Image: "sandbox-api:development-0.1.0-1a2b3c4"))
        .Add(new FakeContainer("api-hml", "heimdall-api-homologation", State: "exited", Image: "heimdall-api:homologation-1.4.0-3f2a9c1"))
        .Add(new FakeContainer("ui-hml", "heimdall-ui-homologation", State: "created", Image: "heimdall-ui:homologation-1.4.0-3f2a9c1"))
        .Add(new FakeContainer("traefik1", "yggdrasil", Service: "traefik", Image: "traefik:v3.5"));

    // By the environment-qualified network aliases; a plain "heimdall-api" would not resolve on a host.
    public FakeProbes Probes { get; } = new FakeProbes()
        .Answer("heimdall-api.production", HttpStatusCode.OK)
        .Answer("heimdall-ui.production", HttpStatusCode.OK)
        .Answer("traefik", HttpStatusCode.OK);

    public List<string> ProbedHosts { get; } = [];

    /// <summary>The applications' detailed health reports, by the same environment-qualified aliases.</summary>
    public FakeProbes Reports { get; } = new FakeProbes();

    public List<string> FetchedHosts { get; } = [];

    public Catalog Catalog { get; init; } = TestData.Catalog();

    public TimeProvider Time { get; init; } = TimeProvider.System;

    /// <summary>Pretend every request arrived on the internal port (TestServer reports port 0).</summary>
    public bool OnInternalPort { get; init; }

    /// <summary>YGGDRASIL_ENVIRONMENTS; the test catalog has local, development, homologation and production.</summary>
    public string Environments { get; init; } = "development,homologation,production";

    /// <summary>YGGDRASIL_ENVIRONMENT, the legacy setting; null leaves it unset.</summary>
    public string? LegacyEnvironment { get; init; }

    protected override void ConfigureWebHost(IWebHostBuilder builder)
    {
        builder.UseSetting("YGGDRASIL_STATUS_TOKEN", TestData.Token);
        builder.UseSetting("YGGDRASIL_ENVIRONMENTS", Environments);
        if (LegacyEnvironment is not null)
        {
            builder.UseSetting("YGGDRASIL_ENVIRONMENT", LegacyEnvironment);
        }

        builder.UseSetting("YGGDRASIL_DOMAIN", "example.com");
        builder.UseSetting("YGGDRASIL_STATUS_CORS_ORIGINS", AllowedOrigin);

        builder.ConfigureTestServices(services =>
        {
            services.AddSingleton(Catalog);
            services.AddSingleton(Time);
            services.AddHttpClient<DockerClient>().ConfigurePrimaryHttpMessageHandler(() => Docker);
            services.AddHttpClient<HealthProber>().ConfigurePrimaryHttpMessageHandler(() => new Recording(Probes, ProbedHosts));
            services.AddHttpClient<DetailedHealthClient>().ConfigurePrimaryHttpMessageHandler(() => new Recording(Reports, FetchedHosts));
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

    public int FetchesOf(string host)
    {
        lock (FetchedHosts)
        {
            return FetchedHosts.Count(fetched => fetched == host);
        }
    }

    private sealed class AlwaysInternal(StatusOptions options) : InternalPortPolicy(options)
    {
        public override bool IsInternal(HttpContext context) => true;
    }

    // Notes the host of every probe, then lets the fake answer it.
    private sealed class Recording(HttpMessageHandler inner, List<string> hosts) : DelegatingHandler(inner)
    {
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
        {
            lock (hosts)
            {
                hosts.Add(request.RequestUri!.Host);
            }

            return base.SendAsync(request, cancellationToken);
        }
    }
}

public class ApiTests : IClassFixture<StatusApiFactory>
{
    // The example of GET /api/status in docs/status-api.md, verbatim: the response must use exactly
    // these property names at every level.
    private const string ContractExample = """
        {
          "host": "example.com",
          "generatedAt": "2026-10-08T18:04:11Z",
          "status": "up",
          "environments": [
            { "id": "development", "name": "Development", "onDemand": true, "status": "stopped" },
            { "id": "homologation", "name": "Homologation", "onDemand": true, "status": "stopped" },
            { "id": "production", "name": "Production", "onDemand": false, "status": "up" }
          ],
          "systems": [
            {
              "id": "heimdall",
              "name": "Heimdall",
              "description": "Identity and access management",
              "icon": "shield",
              "status": "up",
              "environments": [
                {
                  "environment": "development",
                  "status": "stopped",
                  "onDemand": true,
                  "integrations": [
                    { "from": "heimdall-api", "system": "hermes", "external": null, "check": "Hermes", "status": "stopped", "reason": null }
                  ],
                  "applications": [
                    {
                      "id": "heimdall-api",
                      "name": "Heimdall API",
                      "kind": "api",
                      "status": "stopped",
                      "url": "https://heimdall-api-dev.example.com",
                      "repository": "https://github.com/acme/heimdall-api",
                      "deployment": {
                        "version": "1.5.0",
                        "commit": "8d01e7a",
                        "deployedAt": "2026-10-07T14:12:40Z",
                        "image": "heimdall-api:development-1.5.0-8d01e7a"
                      },
                      "container": {
                        "state": "exited",
                        "health": null,
                        "startedAt": null,
                        "restartCount": null
                      },
                      "probe": null,
                      "details": { "state": "not_checked", "checkedAt": null, "reason": null, "status": null, "services": [] }
                    }
                  ]
                },
                {
                  "environment": "homologation",
                  "status": "stopped",
                  "onDemand": true,
                  "integrations": [
                    { "from": "heimdall-api", "system": "hermes", "external": null, "check": "Hermes", "status": "stopped", "reason": null }
                  ],
                  "applications": [
                    {
                      "id": "heimdall-api",
                      "name": "Heimdall API",
                      "kind": "api",
                      "status": "stopped",
                      "url": "https://heimdall-api-hml.example.com",
                      "repository": "https://github.com/acme/heimdall-api",
                      "deployment": {
                        "version": "1.4.0",
                        "commit": "3f2a9c1",
                        "deployedAt": "2026-10-01T09:30:12Z",
                        "image": "heimdall-api:homologation-1.4.0-3f2a9c1"
                      },
                      "container": {
                        "state": "exited",
                        "health": null,
                        "startedAt": null,
                        "restartCount": null
                      },
                      "probe": null,
                      "details": { "state": "not_checked", "checkedAt": null, "reason": null, "status": null, "services": [] }
                    }
                  ]
                },
                {
                  "environment": "production",
                  "status": "up",
                  "onDemand": false,
                  "integrations": [
                    { "from": "heimdall-api", "system": "hermes", "external": null, "check": "Hermes", "status": "down", "reason": "connection refused" }
                  ],
                  "applications": [
                    {
                      "id": "heimdall-api",
                      "name": "Heimdall API",
                      "kind": "api",
                      "status": "up",
                      "url": "https://heimdall-api.example.com",
                      "repository": "https://github.com/acme/heimdall-api",
                      "deployment": {
                        "version": "1.4.0",
                        "commit": "3f2a9c1",
                        "deployedAt": "2026-10-02T21:40:02Z",
                        "image": "heimdall-api:production-1.4.0-3f2a9c1"
                      },
                      "container": {
                        "state": "running",
                        "health": "healthy",
                        "startedAt": "2026-10-02T21:40:05Z",
                        "restartCount": null
                      },
                      "probe": {
                        "healthy": true,
                        "statusCode": 200,
                        "latencyMs": 12,
                        "checkedAt": "2026-10-08T18:04:09Z",
                        "error": null
                      },
                      "details": {
                        "state": "reported",
                        "checkedAt": "2026-10-08T18:04:10Z",
                        "reason": null,
                        "status": "Degraded",
                        "services": [
                          { "name": "Database", "status": "Healthy", "reason": null },
                          { "name": "Hermes", "status": "Unhealthy", "reason": "connection refused" }
                        ]
                      }
                    }
                  ]
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

        Assert.Equal("example.com", (string?)body["host"]);
        Assert.Matches(@"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", (string?)body["generatedAt"]);
        Assert.Equal("up", (string?)body["status"]);
        Assert.Equal(
            """[{"id":"development","name":"Development","onDemand":true,"status":"stopped"},""" +
            """{"id":"homologation","name":"Homologation","onDemand":true,"status":"stopped"},""" +
            """{"id":"production","name":"Production","onDemand":false,"status":"up"}]""",
            body["environments"]!.ToJsonString());

        var heimdall = body["systems"]![0]!;
        Assert.Equal("up", (string?)heimdall["status"]);
        Assert.Equal(["development:stopped", "homologation:stopped", "production:up"],
            heimdall["environments"]!.AsArray().Select(e => $"{e!["environment"]}:{e["status"]}"));

        var api = Application(body, "heimdall", "production", "heimdall-api");
        Assert.Equal("up", (string?)api["status"]);
        Assert.Equal("api", (string?)api["kind"]);
        Assert.Equal("https://heimdall-api.example.com", (string?)api["url"]);
        Assert.Equal("https://github.com/artur-rios/heimdall-api", (string?)api["repository"]);
        Assert.Equal("1.4.0", (string?)api["deployment"]!["version"]);
        Assert.Equal("heimdall-api:production-1.4.0-3f2a9c1", (string?)api["deployment"]!["image"]);
        Assert.Equal("2026-09-17T21:40:05Z", (string?)api["container"]!["startedAt"]);
        Assert.True(api["container"]!.AsObject().ContainsKey("restartCount"));
        Assert.Null(api["container"]!["restartCount"]);

        // Stopped in an on-demand environment: neutral, with the container but no probe.
        var development = Application(body, "heimdall", "development", "heimdall-api");
        Assert.Equal("stopped", (string?)development["status"]);
        Assert.Equal("https://heimdall-api-dev.example.com", (string?)development["url"]);
        Assert.Equal("exited", (string?)development["container"]!["state"]);
        Assert.True(development.AsObject().ContainsKey("probe"));
        Assert.Null(development["probe"]);
        Assert.Equal("https://heimdall-preview.example.com", (string?)Application(body, "heimdall", "development", "heimdall-ui")["url"]);
        Assert.Equal("stopped", (string?)Application(body, "heimdall", "homologation", "heimdall-ui")["status"]);

        // The platform system in every environment: traefik up, jenkins not on this host and so neutral.
        var yggdrasil = body["systems"]![1]!;
        Assert.Equal("up", (string?)yggdrasil["status"]);
        Assert.Equal(TestData.HostEnvironments, yggdrasil["environments"]!.AsArray().Select(e => (string?)e!["environment"]));
        var jenkins = Application(body, "yggdrasil", "development", "jenkins");
        Assert.Equal("not_deployed", (string?)jenkins["status"]);
        Assert.Null(jenkins["deployment"]);
        Assert.Null(jenkins["repository"]);
        Assert.Equal("https://jenkins.example.com", (string?)jenkins["url"]);
        // Written as null, not left out.
        Assert.True(jenkins.AsObject().ContainsKey("container"));
        Assert.True(jenkins.AsObject().ContainsKey("probe"));
        Assert.Equal("up", (string?)Application(body, "yggdrasil", "homologation", "traefik")["status"]);
    }

    [Fact]
    public async Task GivenTheHost_WhenRefreshed_ThenEachApplicationIsLookedForUnderItsEnvironmentsProjectAndProbedAtItsAlias()
    {
        await using var host = new StatusApiFactory();
        await host.ReadyClientAsync();

        var requests = host.Docker.Requests.ToList();
        Assert.Contains(requests, r => r.Contains(Uri.EscapeDataString("com.docker.compose.project=heimdall-api-production")));
        Assert.Contains(requests, r => r.Contains(Uri.EscapeDataString("com.docker.compose.project=heimdall-api-development")));
        Assert.DoesNotContain(requests, r => r.Contains(Uri.EscapeDataString("com.docker.compose.project=heimdall-api\"")));

        // Running ones at their environment's alias, platform components at their plain name, and
        // stopped ones not at all.
        var probed = host.ProbedHosts.ToList();
        Assert.Contains("heimdall-api.production", probed);
        Assert.Contains("heimdall-ui.production", probed);
        Assert.DoesNotContain(probed, h => h.EndsWith(".development", StringComparison.Ordinal) || h.EndsWith(".homologation", StringComparison.Ordinal));
        Assert.DoesNotContain("heimdall-api", probed);
        // A platform component once per refresh, not once per environment: as often as one application
        // in one environment.
        Assert.Equal(probed.Count(h => h == "heimdall-api.production"), probed.Count(h => h == "traefik"));
        Assert.Equal(requests.Count(r => r.Contains("heimdall-api-production")),
            requests.Count(r => r.Contains(Uri.EscapeDataString("com.docker.compose.service=traefik"))));
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
    public async Task GivenApplicationsNotInAnEnvironment_WhenGettingStatus_ThenTheyAreLeftOutOfItAndSystemsOnlyListTheirEnvironments()
    {
        var client = await factory.ReadyClientAsync();

        var body = await GetJson(client, "/api/status");

        // scratch (local only) is on no environment of this host; sandbox only in development.
        Assert.Equal(["heimdall", "yggdrasil", "sandbox"], body["systems"]!.AsArray().Select(s => (string?)s!["id"]));
        var heimdall = body["systems"]![0]!["environments"]!.AsArray();
        Assert.Equal(["heimdall-api", "heimdall-ui", "heimdall-worker"], heimdall[0]!["applications"]!.AsArray().Select(a => (string?)a!["id"]));
        Assert.Equal(["heimdall-api", "heimdall-ui"], heimdall[2]!["applications"]!.AsArray().Select(a => (string?)a!["id"]));
        // heimdall-worker has no container in homologation: listed, as not_deployed.
        Assert.Equal("not_deployed", (string?)Application(body, "heimdall", "homologation", "heimdall-worker")["status"]);

        var sandbox = await GetJson(client, "/api/systems/sandbox");
        Assert.Equal(["development"], sandbox["environments"]!.AsArray().Select(e => (string?)e!["environment"]));
        Assert.Equal("stopped", (string?)sandbox["status"]);

        using var scratch = await client.SendAsync(Authorized("/api/systems/scratch"));
        Assert.Equal(HttpStatusCode.NotFound, scratch.StatusCode);

        // Never looked for either: nothing on this host is supposed to run it.
        Assert.DoesNotContain(factory.Docker.Requests.ToList(), r => r.Contains("scratch-api"));
    }

    [Fact]
    public async Task GivenEnvironmentsInAnotherOrder_WhenGettingStatus_ThenTheyAreReportedInCatalogOrder()
    {
        await using var host = new StatusApiFactory { Environments = "production, development" };
        var client = await host.ReadyClientAsync();

        var body = await GetJson(client, "/api/status");

        Assert.Equal(["development", "production"], body["environments"]!.AsArray().Select(e => (string?)e!["id"]));
        Assert.Equal(["development", "production"],
            body["systems"]![0]!["environments"]!.AsArray().Select(e => (string?)e!["environment"]));
        Assert.DoesNotContain(host.Docker.Requests.ToList(), r => r.Contains("homologation"));
    }

    [Fact]
    public async Task GivenOnlyTheLegacyEnvironment_WhenGettingStatus_ThenItIsTheHostsOnlyEnvironment()
    {
        await using var legacy = new StatusApiFactory { Environments = "", LegacyEnvironment = "production" };
        var client = await legacy.ReadyClientAsync();

        var body = await GetJson(client, "/api/status");

        Assert.Equal("""[{"id":"production","name":"Production","onDemand":false,"status":"up"}]""", body["environments"]!.ToJsonString());
        Assert.Equal(["heimdall", "yggdrasil"], body["systems"]!.AsArray().Select(s => (string?)s!["id"]));
        Assert.Equal("up", (string?)body["status"]);
    }

    [Theory]
    [InlineData("staging,production", null, "YGGDRASIL_ENVIRONMENTS: 'staging' is not an environment in the catalog (local, development, homologation, production)")]
    [InlineData("", "staging", "YGGDRASIL_ENVIRONMENT: 'staging' is not an environment in the catalog (local, development, homologation, production)")]
    public async Task GivenAnEnvironmentNotInTheCatalog_WhenStarting_ThenStartupIsRefusedWithTheReason(string environments, string? legacy, string expected)
    {
        await using var staging = new StatusApiFactory { Environments = environments, LegacyEnvironment = legacy };
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
        Assert.Contains(expected, stderr.ToString());
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
        Assert.Equal(8, targets.Count);
        Assert.Equal("""{"targets":["heimdall-api.development:9464"],"labels":{"system":"heimdall","app":"heimdall-api","kind":"api","environment":"development"}}""",
            targets[0]!.ToJsonString());
        Assert.Equal("""{"targets":["jenkins:8080"],"labels":{"system":"yggdrasil","app":"jenkins","kind":"platform","__metrics_path__":"/prometheus/"}}""",
            targets[6]!.ToJsonString());
        Assert.Equal(
        [
            "heimdall-api.development", "heimdall-api.homologation", "heimdall-api.production",
            "heimdall-worker.development", "heimdall-worker.homologation",
            "traefik", "jenkins",
            "sandbox-api.development",
        ], targets.Select(t => (string?)t!["labels"]!["app"] + (t["labels"]!["environment"] is { } e ? $".{e}" : "")));
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

        Assert.Equal("unknown", (string?)Application(body, "heimdall", "production", "heimdall-api")["status"]);
        // Without Docker a stopped application can't be told from a broken one: its probe fails.
        Assert.Equal("down", (string?)Application(body, "heimdall", "development", "heimdall-api")["status"]);
        // Jenkins does not answer its probe (no such host) and Docker can't say it isn't deployed.
        Assert.Equal("down", (string?)Application(body, "yggdrasil", "production", "jenkins")["status"]);
        Assert.Equal("degraded", (string?)body["status"]);
    }

    [Fact]
    public async Task GivenNoToken_WhenRefreshPosted_Then401()
    {
        var client = await factory.ReadyClientAsync();

        using var response = await client.PostAsync(RefreshPath, null);

        Assert.Equal(HttpStatusCode.Unauthorized, response.StatusCode);
        Assert.Equal("", await response.Content.ReadAsStringAsync());
    }

    [Fact]
    public async Task GivenNotReady_WhenRefreshPosted_Then503WithRetryAfter()
    {
        await using var starting = new StatusApiFactory();
        starting.Docker.Hold = new TaskCompletionSource();
        var client = starting.CreateClient();

        using var response = await client.SendAsync(Authorized(RefreshPath, HttpMethod.Post));

        Assert.Equal(HttpStatusCode.ServiceUnavailable, response.StatusCode);
        Assert.Equal("5", response.Headers.GetValues("Retry-After").Single());
        Assert.Empty(starting.FetchedHosts);
    }

    [Fact]
    public async Task GivenReportingApi_WhenRefreshPosted_ThenBodyHasDetails()
    {
        await using var host = Reporting();
        var client = await host.ReadyClientAsync();

        var body = await PostRefresh(client);

        var details = Application(body, "heimdall", "production", "heimdall-api")["details"]!;
        Assert.Equal("reported", (string?)details["state"]);
        Assert.Equal("Healthy", (string?)details["status"]);
        Assert.Equal("""[{"name":"Database","status":"Healthy","reason":null}]""", details["services"]!.ToJsonString());
        Assert.Equal("example.com", (string?)body["host"]);
    }

    [Fact]
    public async Task GivenNoRefreshPosted_ThenNoReportFetchedAfterSeveralRefreshes()
    {
        await using var host = Reporting();
        await host.ReadyClientAsync();
        var refresher = host.Services.GetRequiredService<StatusRefresher>();

        await refresher.RefreshAsync(CancellationToken.None);
        await refresher.RefreshAsync(CancellationToken.None);
        await refresher.RefreshAsync(CancellationToken.None);

        Assert.Empty(host.FetchedHosts);
    }

    [Fact]
    public async Task GivenStoppedApplication_WhenRefreshPosted_ThenItsReportNotFetched()
    {
        await using var host = Reporting();
        var client = await host.ReadyClientAsync();

        var body = await PostRefresh(client);

        // Not development or homologation (stopped), not jenkins (not deployed), nor anything without a report.
        Assert.Equal(["heimdall-api.production"], host.FetchedHosts);
        Assert.Equal("not_checked", (string?)Application(body, "heimdall", "development", "heimdall-api")["details"]!["state"]);
        Assert.Equal("not_checked", (string?)Application(body, "yggdrasil", "production", "jenkins")["details"]!["state"]);
    }

    [Fact]
    public async Task GivenTwoConcurrentPosts_ThenOneFetchPerApplication()
    {
        await using var host = Reporting();
        var client = await host.ReadyClientAsync();
        var held = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        host.Reports.Hold = held;

        var first = client.SendAsync(Authorized(RefreshPath, HttpMethod.Post));
        await WaitUntil(() => host.FetchesOf("heimdall-api.production") == 1);
        var second = client.SendAsync(Authorized(RefreshPath, HttpMethod.Post));
        // Time for the second to reach the fetch under way. Too little could only let a broken
        // single-flight pass, by turning this into the 10 s reuse; it can never fail a working one.
        await Task.Delay(200);
        held.SetResult();

        using var firstAnswer = await first;
        using var secondAnswer = await second;
        Assert.Equal(HttpStatusCode.OK, firstAnswer.StatusCode);
        Assert.Equal(HttpStatusCode.OK, secondAnswer.StatusCode);
        Assert.Equal(1, host.FetchesOf("heimdall-api.production"));
        var body = JsonNode.Parse(await secondAnswer.Content.ReadAsStringAsync())!;
        Assert.Equal("reported", (string?)Application(body, "heimdall", "production", "heimdall-api")["details"]!["state"]);
    }

    [Fact]
    public async Task GivenPostWithin10Seconds_ThenNoNewFetch()
    {
        var time = new FixedTime(TestData.Now);
        await using var host = Reporting(time);
        var client = await host.ReadyClientAsync();

        await PostRefresh(client);
        time.Now += TimeSpan.FromMilliseconds(9999);
        var body = await PostRefresh(client);

        Assert.Equal(1, host.FetchesOf("heimdall-api.production"));
        Assert.Equal("reported", (string?)Application(body, "heimdall", "production", "heimdall-api")["details"]!["state"]);
    }

    [Fact]
    public async Task GivenPostAfter10Seconds_ThenFetchesAgain()
    {
        var time = new FixedTime(TestData.Now);
        await using var host = Reporting(time);
        var client = await host.ReadyClientAsync();

        await PostRefresh(client);
        time.Now += TimeSpan.FromSeconds(10);
        await PostRefresh(client);

        Assert.Equal(2, host.FetchesOf("heimdall-api.production"));
    }

    [Fact]
    public async Task GivenASlowFetch_WhenPostedRightAfterIt_ThenThe10SecondsCountFromItsEnd()
    {
        var time = new FixedTime(TestData.Now);
        await using var host = Reporting(time);
        var client = await host.ReadyClientAsync();
        var held = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        host.Reports.Hold = held;

        var first = client.SendAsync(Authorized(RefreshPath, HttpMethod.Post));
        await WaitUntil(() => host.FetchesOf("heimdall-api.production") == 1);
        time.Now += TimeSpan.FromSeconds(20);
        held.SetResult();
        (await first).Dispose();
        await PostRefresh(client);

        Assert.Equal(1, host.FetchesOf("heimdall-api.production"));
    }

    [Fact]
    public async Task GivenAFaultedFetch_WhenRefreshedAgain_ThenItFetchesAgain()
    {
        await using var host = Reporting();
        await host.ReadyClientAsync();
        var refresher = host.Services.GetRequiredService<DetailedHealthRefresher>();
        var faulted = new TaskCompletionSource();
        faulted.SetException(new InvalidOperationException("not a failure the client turns into a state"));
        host.Reports.Hold = faulted;

        await Assert.ThrowsAsync<InvalidOperationException>(refresher.RefreshAsync);
        host.Reports.Hold = null;
        await refresher.RefreshAsync();

        Assert.Equal(2, host.FetchesOf("heimdall-api.production"));
        Assert.Equal(DetailsStates.Reported, host.Services.GetRequiredService<DetailedHealthStore>().Current["heimdall-api.production"].State);
    }

    [Fact]
    public async Task GivenOneReportWithAnUnknownCharset_WhenRefreshPosted_ThenTheOthersStillReport()
    {
        await using var host = Reporting();
        host.Docker.Add(new FakeContainer("jenkins1", "yggdrasil", Service: "jenkins", Image: "jenkins:lts"));
        host.Probes.Answer("jenkins", HttpStatusCode.OK);
        host.Reports.Answer("jenkins", HttpStatusCode.OK, """{"status":"Healthy","services":[]}""", "application/json; charset=bogus");
        var client = await host.ReadyClientAsync();

        var body = await PostRefresh(client);

        Assert.Equal("reported", (string?)Application(body, "heimdall", "production", "heimdall-api")["details"]!["state"]);
        var jenkins = Application(body, "yggdrasil", "production", "jenkins")["details"]!;
        Assert.Equal("unavailable", (string?)jenkins["state"]);
        Assert.Equal("invalid response", (string?)jenkins["reason"]);
    }

    [Fact]
    public async Task GivenTheClientDisconnects_WhenFetching_ThenTheFetchStillFinishes()
    {
        await using var host = Reporting();
        var client = await host.ReadyClientAsync();
        var held = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        host.Reports.Hold = held;
        using var disconnect = new CancellationTokenSource();

        var post = client.SendAsync(Authorized(RefreshPath, HttpMethod.Post), disconnect.Token);
        await WaitUntil(() => host.FetchesOf("heimdall-api.production") == 1);
        await disconnect.CancelAsync();
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => post);
        held.SetResult();

        // Another console may be waiting on this same fetch: a request's token would have cancelled it.
        var details = host.Services.GetRequiredService<DetailedHealthStore>();
        await WaitUntil(() => details.Current.ContainsKey("heimdall-api.production"));
    }

    [Fact]
    public async Task GivenAllowedOrigin_WhenPreflightPost_ThenAllowed()
    {
        var client = await factory.ReadyClientAsync();

        using var response = await client.SendAsync(Preflight(StatusApiFactory.AllowedOrigin, RefreshPath, "POST"));

        Assert.Equal(HttpStatusCode.NoContent, response.StatusCode);
        Assert.Equal(StatusApiFactory.AllowedOrigin, response.Headers.GetValues("Access-Control-Allow-Origin").Single());
        Assert.Contains("POST", response.Headers.GetValues("Access-Control-Allow-Methods").Single());
    }

    [Fact]
    public async Task GivenRefreshAlreadyRan_WhenGetStatus_ThenDetailsAreThere()
    {
        await using var host = Reporting();
        var client = await host.ReadyClientAsync();

        await PostRefresh(client);
        var afterPost = await GetJson(client, "/api/status");
        // The 15 s loop builds the next snapshot from the kept details, without fetching.
        await host.Services.GetRequiredService<StatusRefresher>().RefreshAsync(CancellationToken.None);
        var afterLiveness = await GetJson(client, "/api/status");

        Assert.Equal("reported", (string?)Application(afterPost, "heimdall", "production", "heimdall-api")["details"]!["state"]);
        Assert.Equal("reported", (string?)Application(afterLiveness, "heimdall", "production", "heimdall-api")["details"]!["state"]);
        Assert.Equal(1, host.FetchesOf("heimdall-api.production"));
    }

    [Fact]
    public async Task GivenALivenessBuildUnderWay_WhenRepublished_ThenTheNewerDetailsAreWhatStaysPublished()
    {
        var pause = new PausingIntegrations();
        await using var host = new StatusApiFactory { Catalog = TestData.CatalogWithReports(pause) };
        var client = await host.ReadyClientAsync();
        var status = host.Services.GetRequiredService<StatusRefresher>();

        // A liveness refresh that has read the (still empty) details and is stopped inside the build.
        pause.Arm();
        var older = Task.Run(() => status.RefreshAsync(CancellationToken.None));
        Assert.True(pause.Reached.Wait(TimeSpan.FromSeconds(10)));

        // Details land meanwhile. Unserialised, this Republish would finish now and the older build
        // would then overwrite it; a short wait can only let that pass, never fail the right order.
        host.Services.GetRequiredService<DetailedHealthStore>().Set(new Dictionary<string, ApplicationDetails>
        {
            ["heimdall-api.production"] = new(DetailsStates.Reported, TestData.Now, null, "Healthy", []),
        });
        var republish = Task.Run(status.Republish);
        await Task.WhenAny(republish, Task.Delay(200));
        pause.Release.Set();
        await Task.WhenAll(older, republish);

        var body = await GetJson(client, "/api/status");
        Assert.Equal("reported", (string?)Application(body, "heimdall", "production", "heimdall-api")["details"]!["state"]);
    }

    private static StatusApiFactory Reporting(TimeProvider? time = null)
    {
        var host = new StatusApiFactory { Catalog = TestData.CatalogWithReports(), Time = time ?? TimeProvider.System };
        host.Reports.Answer("heimdall-api.production", HttpStatusCode.OK, """{"status":"Healthy","services":[{"name":"Database","status":"Healthy"}]}""");
        return host;
    }

    private static async Task WaitUntil(Func<bool> condition)
    {
        var deadline = DateTime.UtcNow.AddSeconds(10);
        while (!condition() && DateTime.UtcNow < deadline)
        {
            await Task.Delay(20);
        }

        Assert.True(condition());
    }

    // Only SnapshotBuilder.Build reads an application's integrations, so this stops one build at a
    // point where it has already been handed the details and has not yet published anything.
    private sealed class PausingIntegrations : IReadOnlyList<IntegrationDefinition>
    {
        private int armed;

        public ManualResetEventSlim Reached { get; } = new();

        public ManualResetEventSlim Release { get; } = new();

        public int Count => 0;

        public IntegrationDefinition this[int index] => throw new ArgumentOutOfRangeException(nameof(index));

        public void Arm() => Volatile.Write(ref armed, 1);

        public IEnumerator<IntegrationDefinition> GetEnumerator()
        {
            // The first build after arming only; the one it is racing must get through once released.
            if (Interlocked.Exchange(ref armed, 0) == 1)
            {
                Reached.Set();
                Release.Wait(TimeSpan.FromSeconds(10));
            }

            return Enumerable.Empty<IntegrationDefinition>().GetEnumerator();
        }

        System.Collections.IEnumerator System.Collections.IEnumerable.GetEnumerator() => GetEnumerator();
    }

    private static JsonNode Application(JsonNode body, string system, string environment, string id) =>
        body["systems"]!.AsArray().Single(s => (string?)s!["id"] == system)!["environments"]!.AsArray()
            .Single(e => (string?)e!["environment"] == environment)!["applications"]!.AsArray()
            .Single(a => (string?)a!["id"] == id)!;

    private const string RefreshPath = "/api/status/refresh";

    private static HttpRequestMessage Authorized(string path, HttpMethod? method = null)
    {
        var request = new HttpRequestMessage(method ?? HttpMethod.Get, path);
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", TestData.Token);
        return request;
    }

    private static HttpRequestMessage Preflight(string origin, string path = "/api/status", string method = "GET")
    {
        var request = new HttpRequestMessage(HttpMethod.Options, path);
        request.Headers.Add("Origin", origin);
        request.Headers.Add("Access-Control-Request-Method", method);
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

    private static async Task<JsonNode> PostRefresh(HttpClient client)
    {
        using var response = await client.SendAsync(Authorized(RefreshPath, HttpMethod.Post));
        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
        Assert.Equal("application/json", response.Content.Headers.ContentType?.MediaType);
        return JsonNode.Parse(await response.Content.ReadAsStringAsync())!;
    }

    // Same property names at every level, and the same JSON kind (object, array, string, number...)
    // wherever both sides have a value; arrays element by element, as far as the shorter one goes.
    private static void AssertSameShape(JsonNode expected, JsonNode actual, string path)
    {
        Assert.True(expected.GetValueKind() == actual.GetValueKind(),
            $"{path}: expected a {expected.GetValueKind()}, got a {actual.GetValueKind()}");

        switch (expected)
        {
            case JsonObject expectedObject:
                var actualObject = actual.AsObject();
                // The order of a JSON object's properties means nothing.
                Assert.True(expectedObject.Select(p => p.Key).Order().SequenceEqual(actualObject.Select(p => p.Key).Order()),
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

            case JsonArray expectedArray:
                foreach (var (expectedItem, actualItem, index) in expectedArray.Zip(actual.AsArray(), Enumerable.Range(0, expectedArray.Count)))
                {
                    if (expectedItem is not null && actualItem is not null)
                    {
                        AssertSameShape(expectedItem, actualItem, $"{path}[{index}]");
                    }
                }

                break;
        }
    }
}
