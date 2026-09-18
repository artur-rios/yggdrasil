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
}

public sealed record EnvironmentStatus(
    string Environment,
    DateTimeOffset GeneratedAt,
    StatusLevel Status,
    IReadOnlyList<SystemStatus> Systems);

public sealed record SystemStatus(
    string Id,
    string Name,
    string Description,
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

public sealed record ContainerInfo(string State, string? Health, DateTimeOffset? StartedAt, int RestartCount);

public sealed record ProbeResult(bool Healthy, int? StatusCode, long LatencyMs, DateTimeOffset CheckedAt, string? Error);

/// <summary>One element of the Prometheus HTTP service discovery response.</summary>
public sealed record ScrapeTargetGroup(IReadOnlyList<string> Targets, IReadOnlyDictionary<string, string> Labels);
