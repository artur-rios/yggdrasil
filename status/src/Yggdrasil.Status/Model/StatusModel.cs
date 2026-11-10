namespace Yggdrasil.Status.Model;

// The response shapes of docs/status-api.md, one record per JSON object. Property names become the
// contract's camelCase through the web JSON defaults; nulls are written, never omitted, because the
// contract promises every field on every application.

public enum StatusLevel
{
    Up,
    Degraded,
    Down,
    NotDeployed,
    Unknown,
    // Stopped normally in an on-demand environment: neutral, like NotDeployed.
    Stopped,
}

/// <summary>GET /api/status: the host, every environment it runs, and every system across them.</summary>
public sealed record HostStatus(
    string Host,
    DateTimeOffset GeneratedAt,
    StatusLevel Status,
    IReadOnlyList<EnvironmentSummary> Environments,
    IReadOnlyList<SystemStatus> Systems);

public sealed record EnvironmentSummary(string Id, string Name, bool OnDemand, StatusLevel Status);

public sealed record SystemStatus(
    string Id,
    string Name,
    string Description,
    StatusLevel Status,
    IReadOnlyList<SystemEnvironmentStatus> Environments);

/// <summary>A system in one environment: its applications there.</summary>
public sealed record SystemEnvironmentStatus(
    string Environment,
    StatusLevel Status,
    IReadOnlyList<ApplicationStatus> Applications);

public sealed record ApplicationStatus(
    string Id,
    string Name,
    ApplicationKind Kind,
    StatusLevel Status,
    string? Url,
    string? Repository,
    DeploymentInfo? Deployment,
    ContainerInfo? Container,
    ProbeResult? Probe);

public sealed record DeploymentInfo(string? Version, string? Commit, string? DeployedAt, string? Image);

// RestartCount is always null: only the container inspect has it, and the inspect also carries every
// container's environment, which the status API must not be able to read. Kept in the contract so a
// console that reads it still finds the field.
public sealed record ContainerInfo(string State, string? Health, DateTimeOffset? StartedAt, int? RestartCount);

public sealed record ProbeResult(bool Healthy, int? StatusCode, long LatencyMs, DateTimeOffset CheckedAt, string? Error);

/// <summary>
/// What an application's detailed health report said, or why there is none. Status and Services are
/// only set when State is "reported"; Reason only when it is "unavailable".
/// </summary>
public sealed record ApplicationDetails(
    string State,
    DateTimeOffset? CheckedAt,
    string? Reason,
    string? Status,
    IReadOnlyList<ReportedService> Services);

public sealed record ReportedService(string Name, string Status, string? Reason);

/// <summary>The wire values of ApplicationDetails.State.</summary>
public static class DetailsStates
{
    public const string Reported = "reported";
    public const string Unavailable = "unavailable";
    public const string Unsupported = "unsupported";
    public const string NotChecked = "not_checked";
}

/// <summary>One element of the Prometheus HTTP service discovery response.</summary>
public sealed record ScrapeTargetGroup(IReadOnlyList<string> Targets, IReadOnlyDictionary<string, string> Labels);
