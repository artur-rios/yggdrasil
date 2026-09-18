namespace Yggdrasil.Status;

/// <summary>
/// catalog.yaml, validated: the environments, every system and the applications each one is made of,
/// in every environment. The service itself only ever reports on one: see <see cref="EnvironmentCatalog"/>.
/// </summary>
public sealed record Catalog(
    string Owner,
    IReadOnlyList<EnvironmentDefinition> Environments,
    IReadOnlyList<SystemDefinition> Systems)
{
    public IEnumerable<ApplicationDefinition> Applications => Systems.SelectMany(system => system.Applications);
}

/// <summary>
/// An environment as far as this service cares. Its deployment options (mode, trigger, approval...)
/// are validated when the catalog is loaded, since the file is refused whole, but used only by
/// Jenkins and deploy.sh.
/// </summary>
public sealed record EnvironmentDefinition(string Id, string Name);

public sealed record SystemDefinition(
    string Id,
    string Name,
    string Description,
    IReadOnlyList<ApplicationDefinition> Applications);

public sealed record ApplicationDefinition(
    string Id,
    string Name,
    ApplicationKind Kind,
    // Already defaulted: the id for applications, null for platform components that name none.
    string? Repository,
    Uri Health,
    string? Metrics,
    string? MetricsPath,
    string? Host,
    IReadOnlyList<string> Checks,
    ContainerSelector Container,
    // Already defaulted too: the ids of the environments it deploys to, in catalog order; every one
    // when the catalog lists none for it.
    IReadOnlyList<string> Environments);

public enum ApplicationKind
{
    Api,
    Web,
    Worker,
    Platform,
}

/// <summary>
/// How the application's containers are found: by the labels Compose puts on every container it
/// creates. Service is null when the project's containers are all this application's.
/// </summary>
public sealed record ContainerSelector(string Project, string? Service);

/// <summary>
/// The catalog as seen from one environment: only the applications deployed to it, and only the
/// systems that still have one. Everything that reports, probes or exposes targets reads this, so an
/// application that is not meant to run here can't show up as "not deployed" or as a dead target.
/// </summary>
public sealed record EnvironmentCatalog(
    EnvironmentDefinition Environment,
    string Owner,
    IReadOnlyList<SystemDefinition> Systems)
{
    public IEnumerable<ApplicationDefinition> Applications => Systems.SelectMany(system => system.Applications);

    /// <summary>Throws <see cref="StartupException"/> when the catalog has no such environment.</summary>
    public static EnvironmentCatalog For(Catalog catalog, string environmentId)
    {
        var environment = catalog.Environments.FirstOrDefault(e => string.Equals(e.Id, environmentId, StringComparison.Ordinal))
            ?? throw new StartupException("invalid configuration:" + System.Environment.NewLine +
                                          $"  - YGGDRASIL_ENVIRONMENT '{environmentId}' is not an environment in the catalog " +
                                          $"({string.Join(", ", catalog.Environments.Select(e => e.Id))})");

        var systems = catalog.Systems
            .Select(system => system with
            {
                Applications = system.Applications.Where(application => application.Environments.Contains(environment.Id)).ToList(),
            })
            .Where(system => system.Applications.Count > 0)
            .ToList();

        return new EnvironmentCatalog(environment, catalog.Owner, systems);
    }
}
