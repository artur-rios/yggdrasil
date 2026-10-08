using Yggdrasil.Status.Docker;

namespace Yggdrasil.Status.Model;

/// <summary>What was observed about one application in one refresh.</summary>
public sealed record Observation(DockerObservation Docker, ProbeResult? Probe);

/// <summary>Turns the catalog plus one round of observations into the response of GET /api/status.</summary>
public sealed class SnapshotBuilder(HostCatalog catalog, StatusOptions options)
{
    // Labels scripts/deploy.sh puts on the containers it starts.
    public const string VersionLabel = "yggdrasil.version";
    public const string CommitLabel = "yggdrasil.commit";
    public const string DeployedAtLabel = "yggdrasil.deployed_at";

    /// <param name="observations">By <see cref="ProbeTarget.Key"/>: one per target of the catalog.</param>
    public HostStatus Build(IReadOnlyDictionary<string, Observation> observations, DateTimeOffset generatedAt)
    {
        ApplicationStatus Application(ApplicationDefinition application, EnvironmentDefinition environment)
        {
            var target = ProbeTarget.Of(application, environment.Id);
            return BuildApplication(target, observations[target.Key], generatedAt);
        }

        // Each system in the host's environments it has an application in, and only those.
        var systems = catalog.Systems
            .Select(system =>
            {
                var environments = catalog.Environments
                    .Select(environment => (environment.Id, Applications: HostCatalog.In(system, environment)
                        .Select(application => Application(application, environment))
                        .ToList()))
                    .Where(environment => environment.Applications.Count > 0)
                    .Select(environment => new SystemEnvironmentStatus(environment.Id,
                        StatusRules.Aggregate(environment.Applications.Select(application => application.Status)), environment.Applications))
                    .ToList();
                return new SystemStatus(system.Id, system.Name, system.Description,
                    StatusRules.Aggregate(environments.Select(environment => environment.Status)), environments);
            })
            .ToList();

        // Over the applications, not the systems: the same rule either way, but this way an environment
        // can't disagree with what its applications show. Platform components are the host's, not the
        // environment's: counted here, their "up" would make an environment whose applications are all
        // stopped look up.
        var summaries = catalog.Environments
            .Select(environment => new EnvironmentSummary(environment.Id, environment.Name, environment.OnDemand,
                StatusRules.Aggregate(systems
                    .SelectMany(system => system.Environments.Where(e => e.Environment == environment.Id))
                    .SelectMany(e => e.Applications)
                    .Where(application => application.Kind != ApplicationKind.Platform)
                    .Select(application => application.Status))))
            .ToList();

        // ...and so the host counts them, once each, beside its environments.
        var platform = catalog.Applications
            .Where(application => application.Kind == ApplicationKind.Platform)
            .Select(application => StatusRules.ForApplication(observations[application.Id].Docker, observations[application.Id].Probe, generatedAt));
        var status = StatusRules.Aggregate(summaries.Select(summary => summary.Status).Concat(platform));

        return new HostStatus(options.Domain, generatedAt, status, summaries, systems);
    }

    public ApplicationStatus BuildApplication(ProbeTarget target, Observation observation, DateTimeOffset now)
    {
        var application = target.Application;
        var container = (observation.Docker as DockerObservation.Found)?.Container;

        return new ApplicationStatus(
            application.Id,
            application.Name,
            application.Kind,
            StatusRules.ForApplication(observation.Docker, observation.Probe, now, target.OnDemand),
            Url(application, target.HostSuffix, options.Domain),
            Repository(application, catalog.Owner),
            container is null ? null : Deployment(container),
            container is null ? null : new ContainerInfo(container.State, container.Health, container.StartedAt, null),
            observation.Probe);
    }

    /// <summary>https://&lt;host&gt;&lt;hostSuffix&gt;.&lt;domain&gt;: heimdall + -dev is heimdall-dev.example.com.</summary>
    public static string? Url(ApplicationDefinition application, string hostSuffix, string domain) =>
        application.Host is null ? null : $"https://{application.Host}{hostSuffix}.{domain}";

    public static string? Repository(ApplicationDefinition application, string owner) =>
        application.Repository is null ? null : $"https://github.com/{owner}/{application.Repository}";

    private static DeploymentInfo Deployment(ContainerDetails container)
    {
        string? Label(string name) =>
            container.Labels.TryGetValue(name, out var value) && !string.IsNullOrWhiteSpace(value) ? value.Trim() : null;

        // Normalised to the contract's timestamp form when it is one; passed through untouched when
        // it isn't, so a label in an unexpected format is still visible rather than dropped.
        var deployedAt = Label(DeployedAtLabel);
        if (DockerClient.ParseDockerTime(deployedAt) is { } parsed)
        {
            deployedAt = Json.FormatTimestamp(parsed);
        }

        return new DeploymentInfo(Label(VersionLabel), Label(CommitLabel), deployedAt, container.Image);
    }
}

/// <summary>
/// The latest snapshot, shared by the refresher (one writer) and every request (many readers).
/// Snapshots are immutable and replaced whole, so a reference swap is all the synchronisation needed.
/// </summary>
public sealed class StatusSnapshotStore
{
    private volatile HostStatus? current;

    /// <summary>Null until the first refresh has completed.</summary>
    public HostStatus? Current => current;

    public void Set(HostStatus snapshot) => current = snapshot;
}
