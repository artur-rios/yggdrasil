using Microsoft.Extensions.Configuration;
using System.Net;
using Yggdrasil.Status.Docker;
using Yggdrasil.Status.Probing;

namespace Yggdrasil.Status.Tests;

public class DockerClientTests
{
    private static readonly DateTimeOffset Now = TestData.Now;

    private static FakeDocker Docker() => new() { Time = new FixedTime(Now) };

    private static DockerClient Client(HttpMessageHandler docker) =>
        new(new HttpClient(docker) { BaseAddress = new Uri("http://docker-proxy:2375/") }, new FixedTime(Now));

    private static async Task<ContainerDetails> Find(FakeContainer container)
    {
        var result = await Client(Docker().Add(container)).FindAsync(new ContainerSelector(container.Project, null), CancellationToken.None);
        return Assert.IsType<DockerObservation.Found>(result).Container;
    }

    [Fact]
    public async Task GivenAComposeProject_WhenFinding_ThenItFiltersByTheComposeLabels()
    {
        var docker = Docker();

        await Client(docker).FindAsync(new ContainerSelector("yggdrasil", "traefik"), CancellationToken.None);

        var request = Uri.UnescapeDataString(docker.Requests.Single());
        Assert.Equal("/containers/json?all=true&filters={\"label\":[\"com.docker.compose.project=yggdrasil\",\"com.docker.compose.service=traefik\"]}", request);
    }

    [Fact]
    public async Task GivenAContainer_WhenFinding_ThenTheContainerListIsTheOnlyRequest()
    {
        // The inspect (/containers/{id}/json) carries the container's environment, i.e. every
        // application's secrets; the proxy refuses it, and nothing here may need it.
        var docker = Docker().Add(new FakeContainer("c1", "heimdall-api", Health: "healthy"));

        var result = await Client(docker).FindAsync(new ContainerSelector("heimdall-api", null), CancellationToken.None);

        Assert.IsType<DockerObservation.Found>(result);
        Assert.StartsWith("/containers/json?", Assert.Single(docker.Requests));
    }

    [Fact]
    public async Task GivenNoMatchingContainer_WhenFinding_ThenNotFound()
    {
        var result = await Client(Docker()).FindAsync(new ContainerSelector("heimdall-api", null), CancellationToken.None);

        Assert.IsType<DockerObservation.NotFound>(result);
    }

    [Fact]
    public async Task GivenAStoppedAndARunningContainer_WhenFinding_ThenTheRunningOneWins()
    {
        var docker = Docker()
            .Add(new FakeContainer("old", "heimdall-api", State: "exited", Created: Now.AddMinutes(-1)))
            .Add(new FakeContainer("new", "heimdall-api", Health: "healthy", Created: Now.AddDays(-1),
                ExtraLabels: new() { ["yggdrasil.version"] = "1.4.0" }));

        var result = await Client(docker).FindAsync(new ContainerSelector("heimdall-api", null), CancellationToken.None);

        var found = Assert.IsType<DockerObservation.Found>(result).Container;
        Assert.Equal("new", found.Id);
        Assert.Equal("running", found.State);
        Assert.Equal("healthy", found.Health);
        Assert.Equal("heimdall-api:1.4.0-3f2a9c1", found.Image);
        Assert.Equal("1.4.0", found.Labels["yggdrasil.version"]);
    }

    [Theory]
    [InlineData("healthy", true)]
    [InlineData("unhealthy", true)]
    [InlineData("starting", true)]
    [InlineData(null, true)]
    [InlineData("healthy", false)]
    [InlineData("unhealthy", false)]
    [InlineData("starting", false)]
    [InlineData(null, false)]
    public async Task GivenAHealthCheck_WhenFinding_ThenHealthIsReadFromTheListOnEveryEngine(string? health, bool listsHealth)
    {
        var found = await Find(new FakeContainer("c1", "heimdall-api", Health: health, ListsHealth: listsHealth));

        Assert.Equal(health, found.Health);
    }

    [Fact]
    public async Task GivenAContainerThatHasNotRestarted_WhenFinding_ThenItStartedWhenItWasCreated()
    {
        var created = Now.AddHours(-30).AddSeconds(-17);

        var found = await Find(new FakeContainer("c1", "heimdall-api", Created: created, StartedAt: created.AddSeconds(1)));

        Assert.False(found.Restarted);
        // The creation time, to the second: the list's "Up 30 hours" is only accurate to the hour.
        Assert.Equal(created, found.StartedAt);
    }

    [Fact]
    public async Task GivenAContainerThatWaitedForItsDependencies_WhenFinding_ThenThatIsNotARestart()
    {
        // Compose creates every container first, then starts each once its depends_on are healthy.
        var created = Now.AddMinutes(-4);

        var found = await Find(new FakeContainer("c1", "heimdall-api", Created: created, StartedAt: created.AddMinutes(3)));

        Assert.False(found.Restarted);
        Assert.Equal(created, found.StartedAt);
    }

    [Theory]
    [InlineData(45)]
    [InlineData(3 * 60 + 20)]
    [InlineData(9 * 60 + 59)]
    [InlineData(5 * 3600)]
    public async Task GivenAContainerStartedAgainLongAfterItWasCreated_WhenFinding_ThenItRestartedAndStartedAtIsFromItsUptime(int uptimeSeconds)
    {
        var started = Now.AddSeconds(-uptimeSeconds);

        var found = await Find(new FakeContainer("c1", "heimdall-api", Created: Now.AddDays(-3), StartedAt: started));

        Assert.True(found.Restarted);
        // As precise as Docker's "Up ..." text, and never earlier than the real start.
        Assert.NotNull(found.StartedAt);
        Assert.InRange(found.StartedAt!.Value, started, started.AddHours(uptimeSeconds >= 3600 ? 0.5 : 0).AddMinutes(1));
    }

    [Theory]
    [InlineData("exited")]
    [InlineData("created")]
    [InlineData("restarting")]
    [InlineData("dead")]
    public async Task GivenAContainerThatIsNotRunning_WhenFinding_ThenItsStartIsUnknown(string state)
    {
        var found = await Find(new FakeContainer("c1", "heimdall-api", State: state, Created: Now.AddDays(-3)));

        Assert.Equal(state, found.State);
        Assert.Null(found.StartedAt);
        Assert.False(found.Restarted);
    }

    [Fact]
    public async Task GivenAPausedContainer_WhenFinding_ThenItsUptimeIsStillRead()
    {
        var created = Now.AddDays(-2);

        var found = await Find(new FakeContainer("c1", "heimdall-api", State: "paused", Created: created));

        Assert.Equal(created, found.StartedAt);
    }

    [Theory]
    [InlineData("Up Less than a second", 0, 1)]
    [InlineData("Up 1 second", 1, 2)]
    [InlineData("Up 45 seconds (healthy)", 45, 46)]
    [InlineData("Up About a minute", 60, 120)]
    [InlineData("Up 7 minutes (health: starting)", 420, 480)]
    [InlineData("Up About an hour", 3600, 5400)]
    [InlineData("Up 5 hours", 4.5 * 3600, 5.5 * 3600)]
    [InlineData("Up 3 days (unhealthy)", 72 * 3600 - 1800, 96 * 3600 - 1800)]
    [InlineData("Up 2 weeks", 336 * 3600 - 1800, 504 * 3600 - 1800)]
    [InlineData("Up 4 months (Paused)", 2880 * 3600 - 1800, 3600 * 3600 - 1800)]
    [InlineData("Up 2 years", 17520 * 3600, 26280 * 3600)]
    public void GivenDockersUpText_WhenParsed_ThenItGivesTheRangeTheUptimeIsIn(string status, double lower, double upper) =>
        Assert.Equal((TimeSpan.FromSeconds(lower), TimeSpan.FromSeconds(upper)), DockerClient.ParseUptime(status));

    [Theory]
    [InlineData("Exited (0) 5 minutes ago")]
    [InlineData("Restarting (1) 2 seconds ago")]
    [InlineData("Created")]
    [InlineData("")]
    [InlineData(null)]
    public void GivenAStatusWithoutUptime_WhenParsed_ThenNull(string? status) =>
        Assert.Null(DockerClient.ParseUptime(status));

    [Fact]
    public void GivenEveryUptimeUpToThreeYears_WhenDockersTextIsParsed_ThenTheRangeHoldsIt()
    {
        // Every second for the first two hours, then every 7 minutes: covers each unit and each edge
        // of go-units' rounding.
        for (var seconds = 0L; seconds < 3L * 365 * 24 * 3600; seconds += seconds < 7200 ? 1 : 421)
        {
            var uptime = TimeSpan.FromSeconds(seconds);
            var range = DockerClient.ParseUptime("Up " + FakeContainer.HumanDuration(uptime));
            Assert.NotNull(range);
            Assert.True(range.Value.Lower <= uptime && uptime < range.Value.Upper,
                $"{uptime} ({FakeContainer.HumanDuration(uptime)}) is not in [{range.Value.Lower}, {range.Value.Upper})");
        }
    }

    [Fact]
    public async Task GivenDockerUnreachable_WhenFinding_ThenUnreachable()
    {
        var result = await Client(new FakeDocker { Unreachable = true }).FindAsync(new ContainerSelector("x", null), CancellationToken.None);

        Assert.Equal("docker proxy connection refused", Assert.IsType<DockerObservation.Unreachable>(result).Error);
    }

    [Fact]
    public async Task GivenTheProxyRefuses_WhenFinding_ThenUnreachableWithTheStatusCode()
    {
        var result = await Client(new ForbiddingProxy()).FindAsync(new ContainerSelector("x", null), CancellationToken.None);

        Assert.Equal("docker proxy answered 403", Assert.IsType<DockerObservation.Unreachable>(result).Error);
    }

    private sealed class ForbiddingProxy : HttpMessageHandler
    {
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken) =>
            Task.FromResult(new HttpResponseMessage(HttpStatusCode.Forbidden));
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
