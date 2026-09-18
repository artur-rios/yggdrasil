using Yggdrasil.Status.Api;
using Yggdrasil.Status.Docker;
using Yggdrasil.Status.Model;

namespace Yggdrasil.Status.Tests;

public class SnapshotBuilderTests
{
    private readonly Catalog catalog = TestData.Catalog();

    private ApplicationDefinition App(string id) => catalog.Applications.Single(a => a.Id == id);

    [Fact]
    public void GivenAHost_WhenBuildingTheUrl_ThenItIsHttpsUnderTheDomain()
    {
        Assert.Equal("https://heimdall-api.hml.example.com", SnapshotBuilder.Url(App("heimdall-api"), "hml.example.com"));
        Assert.Null(SnapshotBuilder.Url(App("traefik"), "example.com"));
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
        var container = new ContainerDetails("c1", "running", "healthy", TestData.Now.AddHours(-1), 0, "heimdall-api:1.4.0-3f2a9c1",
            new Dictionary<string, string>
            {
                ["yggdrasil.version"] = "1.4.0",
                ["yggdrasil.commit"] = "3f2a9c1",
                ["yggdrasil.deployed_at"] = "2026-09-17T21:40:02+00:00",
            });

        var app = builder.BuildApplication(App("heimdall-api"),
            new Observation(new DockerObservation.Found(container), TestData.Probe()), TestData.Now);

        Assert.Equal(StatusLevel.Up, app.Status);
        Assert.Equal(new DeploymentInfo("1.4.0", "3f2a9c1", "2026-09-17T21:40:02Z", "heimdall-api:1.4.0-3f2a9c1"), app.Deployment);
        Assert.Equal(new ContainerInfo("running", "healthy", TestData.Now.AddHours(-1), 0), app.Container);
    }

    [Fact]
    public void GivenAPlatformContainerWithoutDeployLabels_WhenBuilt_ThenEachDeploymentFieldIsNullButImage()
    {
        var builder = new SnapshotBuilder(catalog, TestData.Options());
        var container = new ContainerDetails("c1", "running", null, TestData.Now.AddHours(-1), 0, "traefik:v3.5", new Dictionary<string, string>());

        var app = builder.BuildApplication(App("traefik"), new Observation(new DockerObservation.Found(container), TestData.Probe()), TestData.Now);

        Assert.Equal(new DeploymentInfo(null, null, null, "traefik:v3.5"), app.Deployment);
    }

    [Fact]
    public void GivenNoContainer_WhenBuilt_ThenDeploymentContainerAndProbeAreNull()
    {
        var builder = new SnapshotBuilder(catalog, TestData.Options());

        var app = builder.BuildApplication(App("jenkins"), new Observation(new DockerObservation.NotFound(), null), TestData.Now);

        Assert.Equal(StatusLevel.NotDeployed, app.Status);
        Assert.Null(app.Deployment);
        Assert.Null(app.Container);
        Assert.Null(app.Probe);
        Assert.Equal("https://jenkins.example.com", app.Url);
    }

    [Fact]
    public void GivenJenkinsNotDeployed_WhenBuilt_ThenThePlatformSystemIsStillUp()
    {
        var builder = new SnapshotBuilder(catalog, TestData.Options());
        var up = new Observation(new DockerObservation.Found(
            new ContainerDetails("c", "running", null, TestData.Now.AddDays(-1), 0, "i", new Dictionary<string, string>())), TestData.Probe());

        var snapshot = builder.Build(new Dictionary<string, Observation>
        {
            ["heimdall-api"] = up,
            ["heimdall-ui"] = new(new DockerObservation.Found(
                new ContainerDetails("c", "exited", null, null, 0, "i", new Dictionary<string, string>())), TestData.Probe(false, statusCode: null, error: "connection refused")),
            ["traefik"] = up,
            ["jenkins"] = new(new DockerObservation.NotFound(), null),
        }, TestData.Now);

        Assert.Equal("production", snapshot.Environment);
        Assert.Equal(TestData.Now, snapshot.GeneratedAt);
        Assert.Equal(StatusLevel.Degraded, snapshot.Systems.Single(s => s.Id == "heimdall").Status);
        Assert.Equal(StatusLevel.Up, snapshot.Systems.Single(s => s.Id == "yggdrasil").Status);
        Assert.Equal(StatusLevel.Degraded, snapshot.Status);
    }

    [Fact]
    public void GivenTheCatalog_WhenListingScrapeTargets_ThenEveryApplicationWithMetricsIsATarget()
    {
        var targets = PrometheusTargets.From(catalog);

        Assert.Equal(["heimdall-api:9464", "traefik:8082", "jenkins:8080"], targets.Select(t => t.Targets.Single()));

        Assert.Equal(new Dictionary<string, string> { ["system"] = "heimdall", ["app"] = "heimdall-api", ["kind"] = "api" },
            targets[0].Labels);
        Assert.Equal(new Dictionary<string, string>
        {
            ["system"] = "yggdrasil", ["app"] = "jenkins", ["kind"] = "platform", ["__metrics_path__"] = "/prometheus/",
        }, targets[2].Labels);
    }
}
