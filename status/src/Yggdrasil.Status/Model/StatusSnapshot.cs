using Yggdrasil.Status.Docker;

namespace Yggdrasil.Status.Model;

/// <summary>What was observed about one application in one refresh.</summary>
public sealed record Observation(DockerObservation Docker, ProbeResult? Probe);

/// <summary>Turns the catalog plus one round of observations into the response of GET /api/status.</summary>
public sealed class SnapshotBuilder(EnvironmentCatalog catalog, StatusOptions options)
{
    // Labels scripts/deploy.sh puts on the containers it starts.
    public const string VersionLabel = "yggdrasil.version";
    public const string CommitLabel = "yggdrasil.commit";
    public const string DeployedAtLabel = "yggdrasil.deployed_at";

    public EnvironmentStatus Build(IReadOnlyDictionary<string, Observation> observations, DateTimeOffset generatedAt)
    {
        var systems = catalog.Systems
            .Select(system =>
            {
                var applications = system.Applications
                    .Select(application => BuildApplication(application, observations[application.Id], generatedAt))
                    .ToList();
                return new SystemStatus(system.Id, system.Name, system.Description,
                    StatusRules.Aggregate(applications.Select(application => application.Status)), applications);
            })
            .ToList();

        // Over the applications, not the systems: the same rule either way, but this way the
        // environment can't disagree with what the applications show.
        var status = StatusRules.Aggregate(systems.SelectMany(system => system.Applications).Select(application => application.Status));

        return new EnvironmentStatus(catalog.Environment.Id, catalog.Environment.Name, generatedAt, status, systems);
    }

    public ApplicationStatus BuildApplication(ApplicationDefinition application, Observation observation, DateTimeOffset now)
    {
        var container = (observation.Docker as DockerObservation.Found)?.Container;

        return new ApplicationStatus(
            application.Id,
            application.Name,
            application.Kind,
            StatusRules.ForApplication(observation.Docker, observation.Probe, now),
            Url(application, options.Domain),
            Repository(application, catalog.Owner),
            container is null ? null : Deployment(container),
            container is null ? null : new ContainerInfo(container.State, container.Health, container.StartedAt, null),
            observation.Probe);
    }

    public static string? Url(ApplicationDefinition application, string domain) =>
        application.Host is null ? null : $"https://{application.Host}.{domain}";

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
    private volatile EnvironmentStatus? current;

    /// <summary>Null until the first refresh has completed.</summary>
    public EnvironmentStatus? Current => current;

    public void Set(EnvironmentStatus snapshot) => current = snapshot;
}
