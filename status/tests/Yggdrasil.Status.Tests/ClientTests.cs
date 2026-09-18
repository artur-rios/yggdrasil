using Microsoft.Extensions.Configuration;
using System.Net;
using Yggdrasil.Status.Docker;
using Yggdrasil.Status.Probing;

namespace Yggdrasil.Status.Tests;

public class DockerClientTests
{
    private static DockerClient Client(FakeDocker docker) =>
        new(new HttpClient(docker) { BaseAddress = new Uri("http://docker-proxy:2375/") });

    [Fact]
    public async Task GivenAComposeProject_WhenFinding_ThenItFiltersByTheComposeLabels()
    {
        var docker = new FakeDocker();

        await Client(docker).FindAsync(new ContainerSelector("yggdrasil", "traefik"), CancellationToken.None);

        var request = Uri.UnescapeDataString(docker.Requests.Single());
        Assert.Equal("/containers/json?all=true&filters={\"label\":[\"com.docker.compose.project=yggdrasil\",\"com.docker.compose.service=traefik\"]}", request);
    }

    [Fact]
    public async Task GivenNoMatchingContainer_WhenFinding_ThenNotFound()
    {
        var result = await Client(new FakeDocker()).FindAsync(new ContainerSelector("heimdall-api", null), CancellationToken.None);

        Assert.IsType<DockerObservation.NotFound>(result);
    }

    [Fact]
    public async Task GivenAStoppedAndARunningContainer_WhenFinding_ThenTheRunningOneWins()
    {
        var docker = new FakeDocker()
            .Add(new FakeContainer("old", "heimdall-api", State: "exited", Created: 2_000))
            .Add(new FakeContainer("new", "heimdall-api", Health: "healthy", RestartCount: 2, Created: 1_000,
                ExtraLabels: new() { ["yggdrasil.version"] = "1.4.0" }));

        var result = await Client(docker).FindAsync(new ContainerSelector("heimdall-api", null), CancellationToken.None);

        var found = Assert.IsType<DockerObservation.Found>(result).Container;
        Assert.Equal("new", found.Id);
        Assert.Equal("running", found.State);
        Assert.Equal("healthy", found.Health);
        Assert.Equal(2, found.RestartCount);
        Assert.Equal(new DateTimeOffset(2026, 9, 17, 21, 40, 5, 123, TimeSpan.Zero).AddTicks(4567), found.StartedAt);
        Assert.Equal("heimdall-api:1.4.0-3f2a9c1", found.Image);
        Assert.Equal("1.4.0", found.Labels["yggdrasil.version"]);
    }

    [Fact]
    public async Task GivenDockerUnreachable_WhenFinding_ThenUnreachable()
    {
        var result = await Client(new FakeDocker { Unreachable = true }).FindAsync(new ContainerSelector("x", null), CancellationToken.None);

        Assert.Equal("docker proxy connection refused", Assert.IsType<DockerObservation.Unreachable>(result).Error);
    }

    [Theory]
    [InlineData("0001-01-01T00:00:00Z", null)]
    [InlineData("", null)]
    [InlineData("2026-09-17T21:40:05Z", "2026-09-17T21:40:05Z")]
    [InlineData("2026-09-17T21:40:05.987654321Z", "2026-09-17T21:40:05.9876543Z")]
    public void GivenADockerTimestamp_WhenParsed_ThenNeverIsNull(string value, string? expected) =>
        Assert.Equal(expected is null ? null : DateTimeOffset.Parse(expected, System.Globalization.CultureInfo.InvariantCulture),
            DockerClient.ParseDockerTime(value));
}

public class HealthProberTests
{
    private static HealthProber Prober(FakeProbes probes) => new(new HttpClient(probes), new FixedTime(TestData.Now));

    [Fact]
    public async Task GivenA2xxAnswer_WhenProbed_ThenHealthy()
    {
        var result = await Prober(new FakeProbes().Answer("app", HttpStatusCode.NoContent)).ProbeAsync(new Uri("http://app/h"), CancellationToken.None);

        Assert.True(result.Healthy);
        Assert.Equal(204, result.StatusCode);
        Assert.Null(result.Error);
        Assert.Equal(TestData.Now, result.CheckedAt);
    }

    [Fact]
    public async Task GivenA5xxAnswer_WhenProbed_ThenUnhealthyWithItsStatusCode()
    {
        var result = await Prober(new FakeProbes().Answer("app", HttpStatusCode.ServiceUnavailable)).ProbeAsync(new Uri("http://app/h"), CancellationToken.None);

        Assert.False(result.Healthy);
        Assert.Equal(503, result.StatusCode);
    }

    [Fact]
    public async Task GivenARefusedConnection_WhenProbed_ThenTheErrorSaysSo()
    {
        var result = await Prober(new FakeProbes().Refuse("app")).ProbeAsync(new Uri("http://app/h"), CancellationToken.None);

        Assert.False(result.Healthy);
        Assert.Null(result.StatusCode);
        Assert.Equal("connection refused", result.Error);
    }

    [Fact]
    public async Task GivenAnUnknownHost_WhenProbed_ThenNameNotResolved()
    {
        var result = await Prober(new FakeProbes()).ProbeAsync(new Uri("http://nowhere/h"), CancellationToken.None);

        Assert.Equal("name not resolved", result.Error);
    }

    [Fact]
    public async Task GivenNoAnswerWithinFiveSeconds_WhenProbed_ThenTimeout()
    {
        var result = await Prober(new FakeProbes().Answer("app", HttpStatusCode.OK, TimeSpan.FromSeconds(30)))
            .ProbeAsync(new Uri("http://app/h"), CancellationToken.None);

        Assert.False(result.Healthy);
        Assert.Equal("timeout", result.Error);
        Assert.InRange(result.LatencyMs, 4900, 7000);
    }
}

public class StatusOptionsTests
{
    private static IConfiguration Config(params (string Key, string Value)[] values)
    {
        var all = new Dictionary<string, string?>
        {
            ["YGGDRASIL_STATUS_TOKEN"] = TestData.Token,
            ["YGGDRASIL_ENVIRONMENT"] = "production",
            ["YGGDRASIL_DOMAIN"] = "example.com",
        };
        foreach (var (key, value) in values)
        {
            all[key] = value;
        }

        return new ConfigurationBuilder().AddInMemoryCollection(all).Build();
    }

    [Fact]
    public void GivenOnlyTheRequiredSettings_WhenLoaded_ThenTheDefaultsApply()
    {
        var options = StatusOptions.Load(Config());

        Assert.Equal("/app/catalog.yaml", options.CatalogPath);
        Assert.Equal(new Uri("http://docker-proxy:2375"), options.DockerUrl);
        Assert.Equal(TimeSpan.FromSeconds(15), options.RefreshInterval);
        Assert.Equal(8081, options.InternalPort);
        Assert.Empty(options.CorsOrigins);
    }

    [Theory]
    [InlineData("", "YGGDRASIL_STATUS_TOKEN is not set")]
    [InlineData("too-short", "must be at least 32 characters (it has 9)")]
    public void GivenAMissingOrShortToken_WhenLoaded_ThenStartupIsRefused(string token, string expected)
    {
        var error = Assert.Throws<StartupException>(() => StatusOptions.Load(Config(("YGGDRASIL_STATUS_TOKEN", token))));

        Assert.Contains(expected, error.Message);
    }

    [Fact]
    public void GivenMissingEnvironmentAndDomain_WhenLoaded_ThenBothAreReported()
    {
        var error = Assert.Throws<StartupException>(() =>
            StatusOptions.Load(Config(("YGGDRASIL_ENVIRONMENT", ""), ("YGGDRASIL_DOMAIN", ""))));

        Assert.Contains("YGGDRASIL_ENVIRONMENT is not set", error.Message);
        Assert.Contains("YGGDRASIL_DOMAIN is not set", error.Message);
    }

    [Fact]
    public void GivenCorsOrigins_WhenLoaded_ThenTheyAreSplitAndNormalised()
    {
        var options = StatusOptions.Load(Config(("YGGDRASIL_STATUS_CORS_ORIGINS", " https://a.example.com , https://b.example.com:8443/,")));

        Assert.Equal(["https://a.example.com", "https://b.example.com:8443"], options.CorsOrigins);
    }

    [Theory]
    [InlineData("YGGDRASIL_STATUS_CORS_ORIGINS", "https://a.example.com/console")]
    [InlineData("YGGDRASIL_STATUS_INTERVAL_SECONDS", "0")]
    [InlineData("YGGDRASIL_STATUS_INTERNAL_PORT", "8080")]
    [InlineData("YGGDRASIL_DOCKER_URL", "docker-proxy:2375")]
    [InlineData("YGGDRASIL_DOMAIN", "https://example.com")]
    public void GivenAnInvalidSetting_WhenLoaded_ThenItIsNamed(string key, string value)
    {
        var error = Assert.Throws<StartupException>(() => StatusOptions.Load(Config((key, value))));

        Assert.Contains(key, error.Message);
    }
}
