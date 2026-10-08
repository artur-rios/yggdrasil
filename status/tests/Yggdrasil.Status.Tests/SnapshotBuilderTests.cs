using Yggdrasil.Status.Api;
using Yggdrasil.Status.Docker;
using Yggdrasil.Status.Model;

namespace Yggdrasil.Status.Tests;

public class SnapshotBuilderTests
{
    private readonly HostCatalog catalog = TestData.OnHost();

    private ApplicationDefinition App(string id) => catalog.Applications.Single(a => a.Id == id);

    private ProbeTarget Target(string id, string environment = "production") => ProbeTarget.Of(App(id), environment);

    private static ContainerDetails Container(string state = "running") =>
        new("c", state, null, state == "running" ? TestData.Now.AddDays(-1) : null, false, "i", new Dictionary<string, string>());

    private static readonly Observation Up = new(new DockerObservation.Found(Container()), TestData.Probe());
    private static readonly Observation Exited = new(new DockerObservation.Found(Container("exited")), null);
    private static readonly Observation NotDeployed = new(new DockerObservation.NotFound(), null);

    [Fact]
    public void GivenAHost_WhenBuildingTheUrl_ThenItIsHttpsUnderTheDomainWithTheSuffix()
    {
        Assert.Equal("https://heimdall-api.hml.example.com", SnapshotBuilder.Url(App("heimdall-api"), "", "hml.example.com"));
        Assert.Equal("https://heimdall-api-dev.example.com", SnapshotBuilder.Url(App("heimdall-api"), "-dev", "example.com"));
        Assert.Null(SnapshotBuilder.Url(App("traefik"), "", "example.com"));
    }

    [Theory]
    [InlineData("heimdall-api", "development", "https://heimdall-api-dev.example.com")]
    [InlineData("heimdall-api", "homologation", "https://heimdall-api-hml.example.com")]
    [InlineData("heimdall-api", "production", "https://heimdall-api.example.com")]
    // The application's own override wins over the environment's suffix.
    [InlineData("heimdall-ui", "development", "https://heimdall-preview.example.com")]
    [InlineData("heimdall-ui", "homologation", "https://heimdall-hml.example.com")]
    // A platform component is one per host, under its plain name in every environment.
    [InlineData("jenkins", "development", "https://jenkins.example.com")]
    public void GivenAnApplicationInAnEnvironment_WhenBuilt_ThenItsUrlHasItsResolvedHostSuffix(string id, string environment, string expected)
    {
        var builder = new SnapshotBuilder(catalog, TestData.Options());

        var app = builder.BuildApplication(Target(id, environment), Up, TestData.Now);

        Assert.Equal(expected, app.Url);
    }

    [Fact]
    public void GivenAnApplicationInAnEnvironment_WhenTargeted_ThenItsProjectHealthAndMetricsCarryTheEnvironment()
    {
        var target = Target("heimdall-api", "homologation");

        Assert.Equal("heimdall-api.homologation", target.Key);
        Assert.Equal(new ContainerSelector("heimdall-api-homologation", null), target.Container);
        Assert.Equal(new Uri("http://heimdall-api.homologation:8080/healthcheck"), target.Health);
        Assert.Equal("heimdall-api.homologation:9464", target.Metrics);
        Assert.Equal("-hml", target.HostSuffix);
        Assert.True(target.OnDemand);
    }

    [Fact]
    public void GivenAnExplicitContainerProject_WhenTargeted_ThenTheEnvironmentIsAppendedToItAndTheServiceKept()
    {
        var target = ProbeTarget.Of(TestData.OnHost("development").Applications.Single(a => a.Id == "sandbox-api"), "development");

        Assert.Equal(new ContainerSelector("sandbox-development", "api"), target.Container);
    }

    [Fact]
    public void GivenAPlatformComponent_WhenTargetedFromAnyEnvironment_ThenItKeepsItsPlainNamesAndIsNeverOnDemand()
    {
        var target = Target("jenkins", "development");

        Assert.Null(target.Environment);
        Assert.Equal("jenkins", target.Key);
        Assert.Equal(new ContainerSelector("yggdrasil", "jenkins"), target.Container);
        Assert.Equal(new Uri("http://jenkins:8080/login"), target.Health);
        Assert.Equal("jenkins:8080", target.Metrics);
        Assert.False(target.OnDemand);
    }

    [Fact]
    public void GivenTheHost_WhenListingTargets_ThenEachApplicationOncePerEnvironmentAndEachPlatformComponentOnce()
    {
        Assert.Equal(
        [
            "heimdall-api.development", "heimdall-api.homologation", "heimdall-api.production",
            "heimdall-ui.development", "heimdall-ui.homologation", "heimdall-ui.production",
            "heimdall-worker.development", "heimdall-worker.homologation",
            "traefik", "jenkins",
            "sandbox-api.development",
        ], catalog.Targets.Select(t => t.Key));
    }

    [Fact]
    public void GivenARepository_WhenBuildingItsUrl_ThenItIsOnGitHubUnderTheOwner()
    {
        Assert.Equal("https://github.com/artur-rios/heimdall-api", SnapshotBuilder.Repository(App("heimdall-api"), "artur-rios"));
        Assert.Null(SnapshotBuilder.Repository(App("jenkins"), "artur-rios"));
    }

    [Fact]
    public void GivenAContainerWithDeployLabels_WhenBuilt_ThenDeploymentAndContainerAreFilled()
    {
        var builder = new SnapshotBuilder(catalog, TestData.Options());
        var container = new ContainerDetails("c1", "running", "healthy", TestData.Now.AddHours(-1), false, "heimdall-api:production-1.4.0-3f2a9c1",
            new Dictionary<string, string>
            {
                ["yggdrasil.version"] = "1.4.0",
                ["yggdrasil.commit"] = "3f2a9c1",
                ["yggdrasil.deployed_at"] = "2026-09-17T21:40:02+00:00",
            });

        var app = builder.BuildApplication(Target("heimdall-api"),
            new Observation(new DockerObservation.Found(container), TestData.Probe()), TestData.Now);

        Assert.Equal(StatusLevel.Up, app.Status);
        Assert.Equal(new DeploymentInfo("1.4.0", "3f2a9c1", "2026-09-17T21:40:02Z", "heimdall-api:production-1.4.0-3f2a9c1"), app.Deployment);
        // restartCount stays null: Docker's container list, all the status API may read, has no count.
        Assert.Equal(new ContainerInfo("running", "healthy", TestData.Now.AddHours(-1), null), app.Container);
    }

    [Fact]
    public void GivenAPlatformContainerWithoutDeployLabels_WhenBuilt_ThenEachDeploymentFieldIsNullButImage()
    {
        var builder = new SnapshotBuilder(catalog, TestData.Options());
        var container = new ContainerDetails("c1", "running", null, TestData.Now.AddHours(-1), false, "traefik:v3.5", new Dictionary<string, string>());

        var app = builder.BuildApplication(Target("traefik"), new Observation(new DockerObservation.Found(container), TestData.Probe()), TestData.Now);

        Assert.Equal(new DeploymentInfo(null, null, null, "traefik:v3.5"), app.Deployment);
    }

    [Fact]
    public void GivenNoContainer_WhenBuilt_ThenDeploymentContainerAndProbeAreNull()
    {
        var builder = new SnapshotBuilder(catalog, TestData.Options());

        var app = builder.BuildApplication(Target("jenkins"), NotDeployed, TestData.Now);

        Assert.Equal(StatusLevel.NotDeployed, app.Status);
        Assert.Null(app.Deployment);
        Assert.Null(app.Container);
        Assert.Null(app.Probe);
        Assert.Equal("https://jenkins.example.com", app.Url);
    }

    [Fact]
    public void GivenAnExitedContainer_WhenBuiltInAnOnDemandEnvironment_ThenStoppedAndElsewhereDown()
    {
        var builder = new SnapshotBuilder(catalog, TestData.Options());

        Assert.Equal(StatusLevel.Stopped, builder.BuildApplication(Target("heimdall-api", "development"), Exited, TestData.Now).Status);
        Assert.Equal(StatusLevel.Down, builder.BuildApplication(Target("heimdall-api", "production"), Exited, TestData.Now).Status);
        // heimdall-worker overrides onDemand to false in homologation.
        Assert.Equal(StatusLevel.Down, builder.BuildApplication(Target("heimdall-worker", "homologation"), Exited, TestData.Now).Status);
        // A platform component serves every environment: never stopped, even shown in an on-demand one.
        Assert.Equal(StatusLevel.Down, builder.BuildApplication(Target("traefik", "development"), Exited, TestData.Now).Status);
    }

    [Fact]
    public void GivenStoppedOnDemandEnvironments_WhenBuilt_ThenEveryLevelRollsUpWithTheOneRule()
    {
        var builder = new SnapshotBuilder(catalog, TestData.Options());

        var snapshot = builder.Build(new Dictionary<string, Observation>
        {
            ["heimdall-api.development"] = Exited,
            ["heimdall-api.homologation"] = Exited,
            ["heimdall-api.production"] = Up,
            ["heimdall-ui.development"] = Exited,
            ["heimdall-ui.homologation"] = NotDeployed,
            ["heimdall-ui.production"] = Up,
            ["heimdall-worker.development"] = Exited,
            ["heimdall-worker.homologation"] = NotDeployed,
            ["traefik"] = Up,
            ["jenkins"] = NotDeployed,
            ["sandbox-api.development"] = Exited,
        }, TestData.Now);

        Assert.Equal("example.com", snapshot.Host);
        Assert.Equal(TestData.Now, snapshot.GeneratedAt);
        Assert.Equal(
            [
                new EnvironmentSummary("development", "Development", true, StatusLevel.Stopped),
                new EnvironmentSummary("homologation", "Homologation", true, StatusLevel.Stopped),
                new EnvironmentSummary("production", "Production", false, StatusLevel.Up),
            ],
            snapshot.Environments);
        Assert.Equal(StatusLevel.Up, snapshot.Status);

        Assert.Equal(["heimdall", "yggdrasil", "sandbox"], snapshot.Systems.Select(s => s.Id));
        var heimdall = snapshot.Systems[0];
        Assert.Equal(StatusLevel.Up, heimdall.Status);
        Assert.Equal(
            [("development", StatusLevel.Stopped), ("homologation", StatusLevel.Stopped), ("production", StatusLevel.Up)],
            heimdall.Environments.Select(e => (e.Environment, e.Status)));
        Assert.Equal(["heimdall-api", "heimdall-ui", "heimdall-worker"], heimdall.Environments[0].Applications.Select(a => a.Id));
        Assert.Equal(["heimdall-api", "heimdall-ui"], heimdall.Environments[2].Applications.Select(a => a.Id));

        // The platform system in every environment, with the one observation of each component.
        var yggdrasil = snapshot.Systems[1];
        Assert.Equal(TestData.HostEnvironments, yggdrasil.Environments.Select(e => e.Environment));
        Assert.All(yggdrasil.Environments, e => Assert.Equal(StatusLevel.Up, e.Status));
        Assert.All(yggdrasil.Environments, e => Assert.Equal(StatusLevel.Up, e.Applications.Single(a => a.Id == "traefik").Status));

        // Only where it has an application: development.
        var sandbox = snapshot.Systems[2];
        Assert.Equal([("development", StatusLevel.Stopped)], sandbox.Environments.Select(e => (e.Environment, e.Status)));
        Assert.Equal(StatusLevel.Stopped, sandbox.Status);
    }

    [Fact]
    public void GivenAProblemInOneEnvironment_WhenBuilt_ThenItsSystemAndTheHostAreDegradedButNotTheOthers()
    {
        var builder = new SnapshotBuilder(catalog, TestData.Options());
        var observations = catalog.Targets.ToDictionary(t => t.Key, _ => Up);
        observations["heimdall-ui.production"] = Exited;

        var snapshot = builder.Build(observations, TestData.Now);

        Assert.Equal(StatusLevel.Degraded, snapshot.Environments.Single(e => e.Id == "production").Status);
        Assert.Equal(StatusLevel.Up, snapshot.Environments.Single(e => e.Id == "development").Status);
        Assert.Equal(StatusLevel.Degraded, snapshot.Systems.Single(s => s.Id == "heimdall").Status);
        Assert.Equal(StatusLevel.Up, snapshot.Systems.Single(s => s.Id == "sandbox").Status);
        Assert.Equal(StatusLevel.Degraded, snapshot.Status);
    }

    [Fact]
    public void GivenAPlatformComponentDown_WhenBuilt_ThenTheHostAndThePlatformSystemShowItButNotTheEnvironments()
    {
        var builder = new SnapshotBuilder(catalog, TestData.Options());
        var observations = catalog.Targets.ToDictionary(t => t.Key, _ => Up);
        observations["traefik"] = Exited;

        var snapshot = builder.Build(observations, TestData.Now);

        // Down in every environment, but beside an up Jenkins: a degraded platform system.
        Assert.All(snapshot.Systems.Single(s => s.Id == "yggdrasil").Environments, e => Assert.Equal(StatusLevel.Degraded, e.Status));
        Assert.All(snapshot.Environments, e => Assert.Equal(StatusLevel.Up, e.Status));
        Assert.Equal(StatusLevel.Degraded, snapshot.Status);
    }

    [Fact]
    public void GivenTheHost_WhenListingScrapeTargets_ThenEachApplicationPerEnvironmentWithItsLabelAndPlatformComponentsOnce()
    {
        var targets = PrometheusTargets.From(catalog);

        Assert.Equal(
        [
            "heimdall-api.development:9464", "heimdall-api.homologation:9464", "heimdall-api.production:9464",
            "heimdall-worker.development:9464", "heimdall-worker.homologation:9464",
            "traefik:8082", "jenkins:8080",
            "sandbox-api.development:9464",
        ], targets.Select(t => t.Targets.Single()));

        Assert.Equal(new Dictionary<string, string>
        {
            ["system"] = "heimdall",
            ["app"] = "heimdall-api",
            ["kind"] = "api",
            ["environment"] = "production",
        }, targets[2].Labels);
        Assert.Equal(new Dictionary<string, string>
        {
            ["system"] = "yggdrasil",
            ["app"] = "jenkins",
            ["kind"] = "platform",
            ["__metrics_path__"] = "/prometheus/",
        }, targets[6].Labels);
    }

    [Fact]
    public void GivenApplicationsNotOnTheHost_WhenListingScrapeTargets_ThenOnlyTheHostsEnvironmentsAreTargets()
    {
        var production = PrometheusTargets.From(TestData.OnHost("production")).Select(t => t.Targets.Single()).ToList();
        var all = PrometheusTargets.From(catalog).Select(t => t.Targets.Single()).ToList();

        Assert.Equal(["heimdall-api.production:9464", "traefik:8082", "jenkins:8080"], production);
        // scratch-api is local only, and local is not one of the host's environments.
        Assert.DoesNotContain(all, t => t.StartsWith("scratch-api", StringComparison.Ordinal));
    }
}
