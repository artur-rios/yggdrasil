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

    private static ApplicationDetails Report(string status, params (string Name, string Status)[] services) =>
        new(DetailsStates.Reported, Now, null, status,
            services.Select(s => new ReportedService(s.Name, s.Status, null)).ToList());

    private static ApplicationDetails Of(string state, string? reason = null) => new(state, Now, reason, null, []);

    [Theory]
    [InlineData(StatusLevel.Stopped, StatusLevel.Up, "Healthy", IntegrationLevel.Stopped)]
    [InlineData(StatusLevel.Up, StatusLevel.Stopped, "Unhealthy", IntegrationLevel.Stopped)]
    [InlineData(StatusLevel.Up, StatusLevel.NotDeployed, "Unhealthy", IntegrationLevel.Stopped)]
    [InlineData(StatusLevel.Up, StatusLevel.Up, "NotConfigured", IntegrationLevel.NotConfigured)]
    [InlineData(StatusLevel.Up, StatusLevel.Up, "Healthy", IntegrationLevel.Up)]
    [InlineData(StatusLevel.Up, StatusLevel.Up, "Degraded", IntegrationLevel.Down)]
    [InlineData(StatusLevel.Up, StatusLevel.Up, "Unhealthy", IntegrationLevel.Down)]
    public void GivenACallerReportingTheCheck_WhenDerivingTheIntegration_ThenTheFirstMatchingRowWins(
        StatusLevel caller, StatusLevel target, string checkStatus, IntegrationLevel expected) =>
        Assert.Equal(expected, StatusRules.ForIntegration(caller, target, Report("Healthy", ("Hermes", checkStatus)), "Hermes"));

    [Fact]
    public void GivenAnExternalTarget_WhenTheCheckIsHealthy_ThenUp() =>
        Assert.Equal(IntegrationLevel.Up, StatusRules.ForIntegration(StatusLevel.Up, null, Report("Healthy", ("Smtp", "Healthy")), "Smtp"));

    [Fact]
    public void GivenACallerThatDidNotAnswer_ThenUnknown() =>
        Assert.Equal(IntegrationLevel.Unknown,
            StatusRules.ForIntegration(StatusLevel.Up, StatusLevel.Up, Of(DetailsStates.Unavailable, "timeout"), "Hermes"));

    [Fact]
    public void GivenAReportWithoutTheCheck_ThenUnknown() =>
        Assert.Equal(IntegrationLevel.Unknown,
            StatusRules.ForIntegration(StatusLevel.Up, StatusLevel.Up, Report("Healthy", ("Database", "Healthy")), "Hermes"));

    [Fact]
    public void GivenNothingFetched_ThenUnknown()
    {
        Assert.Equal(IntegrationLevel.Unknown, StatusRules.ForIntegration(StatusLevel.Up, StatusLevel.Up, null, "Hermes"));
        Assert.Equal(IntegrationLevel.Unknown,
            StatusRules.ForIntegration(StatusLevel.Up, StatusLevel.Up, Of(DetailsStates.NotChecked), "Hermes"));
    }

    private static readonly IReadOnlySet<string> NoneStopped = new HashSet<string>();

    [Theory]
    [InlineData("Healthy", StatusLevel.Up)]
    [InlineData("Degraded", StatusLevel.Degraded)]
    [InlineData("Unhealthy", StatusLevel.Degraded)]
    public void GivenAnUpApplicationAndAReport_WhenApplyingIt_ThenItCanOnlyWorsenToDegraded(string reported, StatusLevel expected) =>
        Assert.Equal(expected, StatusRules.WithDetails(StatusLevel.Up, Report(reported, ("Database", reported)), NoneStopped));

    [Fact]
    public void GivenAnUpApplicationThatDidNotAnswer_ThenDegraded() =>
        Assert.Equal(StatusLevel.Degraded,
            StatusRules.WithDetails(StatusLevel.Up, Of(DetailsStates.Unavailable, "timeout"), NoneStopped));

    [Fact]
    public void GivenAnUpApplicationWithoutTheRoute_OrNotChecked_ThenUp()
    {
        Assert.Equal(StatusLevel.Up, StatusRules.WithDetails(StatusLevel.Up, Of(DetailsStates.Unsupported), NoneStopped));
        Assert.Equal(StatusLevel.Up, StatusRules.WithDetails(StatusLevel.Up, Of(DetailsStates.NotChecked), NoneStopped));
        Assert.Equal(StatusLevel.Up, StatusRules.WithDetails(StatusLevel.Up, null, NoneStopped));
    }

    [Fact]
    public void GivenALivenessThatIsNotUp_WhenApplyingAReport_ThenLivenessPrecedes()
    {
        Assert.Equal(StatusLevel.Down, StatusRules.WithDetails(StatusLevel.Down, Report("Healthy"), NoneStopped));
        Assert.Equal(StatusLevel.Stopped, StatusRules.WithDetails(StatusLevel.Stopped, Report("Unhealthy", ("Hermes", "Unhealthy")), NoneStopped));
    }

    [Fact]
    public void GivenEveryFailingEntryIsAStoppedIntegration_ThenTheReportCountsAsHealthy() =>
        Assert.Equal(StatusLevel.Up, StatusRules.WithDetails(StatusLevel.Up,
            Report("Degraded", ("Database", "Healthy"), ("Hermes", "Unhealthy")), new HashSet<string> { "Hermes" }));

    [Fact]
    public void GivenAnotherEntryFailsBesideAStoppedIntegration_ThenDegraded() =>
        Assert.Equal(StatusLevel.Degraded, StatusRules.WithDetails(StatusLevel.Up,
            Report("Unhealthy", ("Database", "Unhealthy"), ("Hermes", "Unhealthy")), new HashSet<string> { "Hermes" }));
}
