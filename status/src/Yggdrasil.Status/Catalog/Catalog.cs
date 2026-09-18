namespace Yggdrasil.Status;

/// <summary>catalog.yaml, validated: every system and the applications each one is made of.</summary>
public sealed record Catalog(string Owner, IReadOnlyList<SystemDefinition> Systems)
{
    public IEnumerable<ApplicationDefinition> Applications => Systems.SelectMany(system => system.Applications);
}

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
    ContainerSelector Container);

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
