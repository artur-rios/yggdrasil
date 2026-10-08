using Yggdrasil.Status.Docker;
using Yggdrasil.Status.Model;

namespace Yggdrasil.Status.Tests;

/// <summary>One test per row and clause of the status table and the roll-up rule in docs/status-api.md.</summary>
public class StatusRulesTests
{
    private static readonly DateTimeOffset Now = TestData.Now;

    private static DockerObservation Running(string? health = null, bool restarted = false, DateTimeOffset? startedAt = null) =>
        new DockerObservation.Found(new ContainerDetails("c1", "running", health, startedAt ?? Now.AddDays(-1), restarted, "img:1",
            new Dictionary<string, string>()));

    private static DockerObservation InState(string state) =>
        new DockerObservation.Found(new ContainerDetails("c1", state, null, null, false, "img:1", new Dictionary<string, string>()));

    [Fact]
    public void GivenARunningContainerWithoutHealthCheckAndA2xxProbe_ThenUp() =>
        Assert.Equal(StatusLevel.Up, StatusRules.ForApplication(Running(), TestData.Probe(), Now));

    [Fact]
    public void GivenARunningHealthyContainerAndA2xxProbe_ThenUp() =>
        Assert.Equal(StatusLevel.Up, StatusRules.ForApplication(Running("healthy"), TestData.Probe(), Now));

    [Fact]
    public void GivenARunningContainerAndAFailingProbe_ThenDegraded() =>
        Assert.Equal(StatusLevel.Degraded, StatusRules.ForApplication(Running(), TestData.Probe(false, statusCode: 503), Now));

    [Fact]
    public void GivenARunningContainerAndAProbeTimeout_ThenDegraded() =>
        Assert.Equal(StatusLevel.Degraded, StatusRules.ForApplication(Running(), TestData.Probe(false, 5000, null, "timeout"), Now));

    [Theory]
    [InlineData(2001, StatusLevel.Degraded)]
    [InlineData(2000, StatusLevel.Up)]
    public void GivenAProbeSlowerThanTwoSeconds_ThenDegraded(long latencyMs, StatusLevel expected) =>
        Assert.Equal(expected, StatusRules.ForApplication(Running(), TestData.Probe(latencyMs: latencyMs), Now));

    [Theory]
    [InlineData("unhealthy")]
    [InlineData("starting")]
    public void GivenDockerReportsUnhealthyOrStarting_ThenDegraded(string health) =>
        Assert.Equal(StatusLevel.Degraded, StatusRules.ForApplication(Running(health), TestData.Probe(), Now));

    [Fact]
    public void GivenARestartInTheLastTenMinutes_ThenDegraded() =>
        Assert.Equal(StatusLevel.Degraded,
            StatusRules.ForApplication(Running(restarted: true, startedAt: Now.AddMinutes(-9)), TestData.Probe(), Now));

    [Fact]
    public void GivenARecentStartInAnOnDemandEnvironment_ThenUp() =>
        Assert.Equal(StatusLevel.Up,
            StatusRules.ForApplication(Running(restarted: true, startedAt: Now.AddMinutes(-1)), TestData.Probe(), Now, onDemand: true));

    [Fact]
    public void GivenARestartMoreThanTenMinutesAgo_ThenUp() =>
        Assert.Equal(StatusLevel.Up,
            StatusRules.ForApplication(Running(restarted: true, startedAt: Now.AddMinutes(-11)), TestData.Probe(), Now));

    [Fact]
    public void GivenAFreshDeployWithNoRestart_ThenUp() =>
        Assert.Equal(StatusLevel.Up,
            StatusRules.ForApplication(Running(startedAt: Now.AddMinutes(-1)), TestData.Probe(), Now));

    [Theory]
    [InlineData("exited")]
    [InlineData("created")]
    [InlineData("restarting")]
    [InlineData("paused")]
    [InlineData("dead")]
    public void GivenAContainerThatIsNotRunning_ThenDown(string state) =>
        Assert.Equal(StatusLevel.Down, StatusRules.ForApplication(InState(state), TestData.Probe(), Now));

    [Theory]
    [InlineData("exited")]
    [InlineData("created")]
    public void GivenAnOnDemandEnvironmentAndAContainerStoppedNormally_ThenStopped(string state)
    {
        Assert.True(StatusRules.IsStopped(InState(state), onDemand: true));
        Assert.Equal(StatusLevel.Stopped, StatusRules.ForApplication(InState(state), null, Now, onDemand: true));
    }

    [Theory]
    [InlineData("exited")]
    [InlineData("created")]
    public void GivenAnEnvironmentNotOnDemandAndAStoppedContainer_ThenDown(string state)
    {
        Assert.False(StatusRules.IsStopped(InState(state), onDemand: false));
        Assert.Equal(StatusLevel.Down, StatusRules.ForApplication(InState(state), TestData.Probe(false, statusCode: null, error: "name not resolved"), Now));
    }

    [Theory]
    [InlineData("restarting")]
    [InlineData("paused")]
    [InlineData("dead")]
    public void GivenAnOnDemandEnvironmentAndAContainerNotStoppedNormally_ThenDown(string state) =>
        Assert.Equal(StatusLevel.Down, StatusRules.ForApplication(InState(state), TestData.Probe(), Now, onDemand: true));

    [Fact]
    public void GivenAnOnDemandEnvironmentAndARunningContainer_ThenItsProbeDecidesAsAnywhere()
    {
        Assert.False(StatusRules.IsStopped(Running(), onDemand: true));
        Assert.Equal(StatusLevel.Up, StatusRules.ForApplication(Running(), TestData.Probe(), Now, onDemand: true));
        Assert.Equal(StatusLevel.Degraded, StatusRules.ForApplication(Running(), TestData.Probe(false, statusCode: 500), Now, onDemand: true));
    }

    [Fact]
    public void GivenAnOnDemandEnvironmentAndNoContainerOrNoDocker_ThenNotStopped()
    {
        Assert.Equal(StatusLevel.NotDeployed, StatusRules.ForApplication(new DockerObservation.NotFound(), null, Now, onDemand: true));
        Assert.Equal(StatusLevel.Down,
            StatusRules.ForApplication(new DockerObservation.Unreachable("x"), TestData.Probe(false, statusCode: null, error: "connection refused"), Now, onDemand: true));
    }

    [Fact]
    public void GivenDockerUnreachableAndAFailingProbe_ThenDown() =>
        Assert.Equal(StatusLevel.Down,
            StatusRules.ForApplication(new DockerObservation.Unreachable("x"), TestData.Probe(false, error: "connection refused", statusCode: null), Now));

    [Fact]
    public void GivenDockerUnreachableAndAPassingProbe_ThenUnknown() =>
        Assert.Equal(StatusLevel.Unknown,
            StatusRules.ForApplication(new DockerObservation.Unreachable("x"), TestData.Probe(), Now));

    [Fact]
    public void GivenNoContainer_ThenNotDeployed() =>
        Assert.Equal(StatusLevel.NotDeployed, StatusRules.ForApplication(new DockerObservation.NotFound(), null, Now));

    // The roll-up, the same at every level: applications, environments, systems, the host.

    [Theory]
    [InlineData(new[] { StatusLevel.Up, StatusLevel.Up }, StatusLevel.Up)]
    [InlineData(new[] { StatusLevel.Up, StatusLevel.NotDeployed }, StatusLevel.Up)]
    [InlineData(new[] { StatusLevel.NotDeployed, StatusLevel.NotDeployed }, StatusLevel.NotDeployed)]
    [InlineData(new StatusLevel[0], StatusLevel.NotDeployed)]
    [InlineData(new[] { StatusLevel.Down, StatusLevel.Down }, StatusLevel.Down)]
    [InlineData(new[] { StatusLevel.Down, StatusLevel.NotDeployed }, StatusLevel.Down)]
    [InlineData(new[] { StatusLevel.Down, StatusLevel.Up }, StatusLevel.Degraded)]
    [InlineData(new[] { StatusLevel.Degraded, StatusLevel.Up }, StatusLevel.Degraded)]
    [InlineData(new[] { StatusLevel.Degraded, StatusLevel.Unknown }, StatusLevel.Degraded)]
    [InlineData(new[] { StatusLevel.Down, StatusLevel.Unknown }, StatusLevel.Degraded)]
    [InlineData(new[] { StatusLevel.Unknown, StatusLevel.Up }, StatusLevel.Unknown)]
    [InlineData(new[] { StatusLevel.Unknown, StatusLevel.NotDeployed }, StatusLevel.Unknown)]
    // Stopped: neutral too, unless every deployed member is stopped.
    [InlineData(new[] { StatusLevel.Stopped }, StatusLevel.Stopped)]
    [InlineData(new[] { StatusLevel.Stopped, StatusLevel.Stopped }, StatusLevel.Stopped)]
    [InlineData(new[] { StatusLevel.Stopped, StatusLevel.NotDeployed }, StatusLevel.Stopped)]
    [InlineData(new[] { StatusLevel.Stopped, StatusLevel.Up }, StatusLevel.Up)]
    [InlineData(new[] { StatusLevel.Stopped, StatusLevel.Stopped, StatusLevel.Up }, StatusLevel.Up)]
    [InlineData(new[] { StatusLevel.Stopped, StatusLevel.Down }, StatusLevel.Down)]
    [InlineData(new[] { StatusLevel.Stopped, StatusLevel.Down, StatusLevel.NotDeployed }, StatusLevel.Down)]
    [InlineData(new[] { StatusLevel.Stopped, StatusLevel.Down, StatusLevel.Up }, StatusLevel.Degraded)]
    [InlineData(new[] { StatusLevel.Stopped, StatusLevel.Degraded }, StatusLevel.Degraded)]
    [InlineData(new[] { StatusLevel.Stopped, StatusLevel.Unknown }, StatusLevel.Unknown)]
    public void GivenMemberStatuses_WhenAggregated_ThenTheWorstWinsAndNotDeployedAndStoppedAreNeutral(StatusLevel[] members, StatusLevel expected) =>
        Assert.Equal(expected, StatusRules.Aggregate(members));

    [Fact]
    public void GivenStoppedOnDemandEnvironmentsBesideARunningOne_WhenAggregatedAsAHost_ThenTheHostIsUp() =>
        // development and homologation stopped, production up, traefik up and jenkins not on this host.
        Assert.Equal(StatusLevel.Up, StatusRules.Aggregate(
            [StatusLevel.Stopped, StatusLevel.Stopped, StatusLevel.Up, StatusLevel.Up, StatusLevel.NotDeployed]));
}
