using System.Globalization;
using System.Net;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace Yggdrasil.Status.Docker;

/// <summary>What Docker says about one application on this host.</summary>
public abstract record DockerObservation
{
    private DockerObservation() { }

    /// <summary>Docker (or the socket proxy) did not answer; the application's state is unknowable.</summary>
    public sealed record Unreachable(string Error) : DockerObservation;

    /// <summary>Docker answered, and no container carries the application's Compose labels.</summary>
    public sealed record NotFound : DockerObservation;

    public sealed record Found(ContainerDetails Container) : DockerObservation;
}

public sealed record ContainerDetails(
    string Id,
    // Docker's own words: created, running, paused, restarting, removing, exited or dead.
    string State,
    // healthy, unhealthy or starting; null when the image has no health check.
    string? Health,
    // When it last started; null when it is not up. For a container that has not restarted, its
    // creation time, to the second (Compose starts it right after creating it, or once its depends_on
    // are healthy); for one that has, as precise as `docker ps`'s "Up ...".
    DateTimeOffset? StartedAt,
    // Started again after it was created: Docker's restart policy after a crash, a `docker restart`,
    // or the Docker engine or host restarting. A redeploy is a new container, so it is not one.
    bool Restarted,
    string? Image,
    IReadOnlyDictionary<string, string> Labels);

/// <summary>
/// Docker Engine API over HTTP, through the docker-proxy service of platform/compose.yml
/// (wollomatic/socket-proxy). The proxy lets exactly one request through, GET /containers/json, and
/// this reads nothing else: the inspect (/containers/{id}/json) would add the restart count and the
/// exact start time, but it also carries every container's environment -- the secrets of every
/// application on the host -- and the status API is reachable from the internet. Its logs, archive
/// and exec endpoints are out of reach for the same reason, and so is anything that writes.
/// </summary>
public sealed partial class DockerClient(HttpClient http, TimeProvider time)
{
    // Compose creates the containers of a project first, then starts each one once its depends_on are
    // healthy; a start this long after the creation is still that first start, not a restart.
    internal static readonly TimeSpan StartAfterCreation = TimeSpan.FromMinutes(5);

    // Only what is read below; the Engine API's documents are much larger.
    private static readonly JsonSerializerOptions JsonOptions = new() { PropertyNameCaseInsensitive = true };

    public async Task<DockerObservation> FindAsync(ContainerSelector selector, CancellationToken cancellationToken)
    {
        try
        {
            var labels = new List<string> { $"com.docker.compose.project={selector.Project}" };
            if (selector.Service is not null)
            {
                labels.Add($"com.docker.compose.service={selector.Service}");
            }

            var filters = JsonSerializer.Serialize(new Dictionary<string, List<string>> { ["label"] = labels });
            var listed = await GetAsync<List<ContainerSummary>>(
                $"containers/json?all=true&filters={Uri.EscapeDataString(filters)}", cancellationToken);

            // "When several containers match, the running one wins": during a deploy the old and the
            // new container can both exist for a moment, and a stopped leftover must not mask the one
            // serving traffic. Among equals, the newest.
            var chosen = listed?
                .OrderByDescending(container => string.Equals(container.State, "running", StringComparison.OrdinalIgnoreCase))
                .ThenByDescending(container => container.Created)
                .FirstOrDefault();

            return chosen is null
                ? new DockerObservation.NotFound()
                : new DockerObservation.Found(ToDetails(chosen, time.GetUtcNow()));
        }
        catch (DockerException e)
        {
            return new DockerObservation.Unreachable(e.Message);
        }
        catch (HttpRequestException e)
        {
            return new DockerObservation.Unreachable(e.HttpRequestError switch
            {
                HttpRequestError.NameResolutionError => "docker proxy name not resolved",
                HttpRequestError.ConnectionError => "docker proxy connection refused",
                _ => "docker proxy request failed",
            });
        }
        catch (TaskCanceledException) when (!cancellationToken.IsCancellationRequested)
        {
            return new DockerObservation.Unreachable("docker proxy timeout");
        }
        catch (JsonException)
        {
            return new DockerObservation.Unreachable("docker proxy returned an unexpected response");
        }
    }

    private async Task<T?> GetAsync<T>(string path, CancellationToken cancellationToken) where T : class
    {
        using var response = await http.GetAsync(path, cancellationToken);
        if (response.StatusCode == HttpStatusCode.NotFound)
        {
            return null;
        }

        if (!response.IsSuccessStatusCode)
        {
            // 403 is the proxy refusing: its allowlist in platform/compose.yml lacks GET /containers/json.
            throw new DockerException($"docker proxy answered {(int)response.StatusCode}");
        }

        await using var body = await response.Content.ReadAsStreamAsync(cancellationToken);
        return await JsonSerializer.DeserializeAsync<T>(body, JsonOptions, cancellationToken)
               ?? throw new JsonException("empty body");
    }

    private static ContainerDetails ToDetails(ContainerSummary summary, DateTimeOffset now)
    {
        // Docker Engine 29 (API 1.52) lists the health; before, it is only in the status text.
        var health = summary.Health is { } listed ? listed.Status : HealthFromStatus(summary.Status);
        // "none" is what Docker reports for a container whose image has no health check.
        if (string.IsNullOrEmpty(health) || health == "none")
        {
            health = null;
        }

        var (startedAt, restarted) = Started(summary, now);

        return new ContainerDetails(
            summary.Id,
            summary.State ?? "unknown",
            health,
            startedAt,
            restarted,
            summary.Image,
            summary.Labels ?? new Dictionary<string, string>());
    }

    private static string? HealthFromStatus(string? status) =>
        status is null ? null : StatusHealth().Match(status) is { Success: true } match ? match.Groups["health"].Value : null;

    // The list has no start time, only "Up 5 minutes", and the creation time to the second. A container
    // whose uptime reaches back to its creation has not restarted, and then the creation time is its
    // start, exactly; one that started well after its creation has, and then the uptime is all there is.
    private static (DateTimeOffset? StartedAt, bool Restarted) Started(ContainerSummary summary, DateTimeOffset now)
    {
        if (ParseUptime(summary.Status) is not { } uptime)
        {
            return (null, false);
        }

        var earliestStart = now - uptime.Upper;
        var latestStart = now - uptime.Lower;
        if (summary.Created <= 0)
        {
            return (latestStart, false);
        }

        var created = DateTimeOffset.FromUnixTimeSeconds(summary.Created);
        if (earliestStart - created > StartAfterCreation)
        {
            // The latest start the text allows, so a restart is never older than it really is.
            return (latestStart, true);
        }

        return (created, false);
    }

    /// <summary>
    /// The range [lower, upper) the uptime is in, from Docker's status text ("Up 5 minutes (healthy)").
    /// Docker writes it with go-units' HumanDuration, which truncates seconds and minutes and rounds
    /// hours; null for a container that is not up ("Exited (0) 5 minutes ago", "Created").
    /// </summary>
    internal static (TimeSpan Lower, TimeSpan Upper)? ParseUptime(string? status)
    {
        if (status is null || UpText().Match(status) is not { Success: true } match)
        {
            return null;
        }

        var amount = match.Groups["amount"].Value;
        if (amount == "Less than a second")
        {
            return (TimeSpan.Zero, TimeSpan.FromSeconds(1));
        }

        if (amount == "About a minute")
        {
            return (TimeSpan.FromMinutes(1), TimeSpan.FromMinutes(2));
        }

        if (amount == "About an hour")
        {
            // Only past 60 minutes ("59 minutes" before), and until the hours round to 2.
            return (TimeSpan.FromHours(1), TimeSpan.FromHours(1.5));
        }

        var n = int.Parse(match.Groups["n"].Value, CultureInfo.InvariantCulture);
        var halfHour = TimeSpan.FromMinutes(30);
        return match.Groups["unit"].Value switch
        {
            "second" => (TimeSpan.FromSeconds(n), TimeSpan.FromSeconds(n + 1)),
            "minute" => (TimeSpan.FromMinutes(n), TimeSpan.FromMinutes(n + 1)),
            // From here on Docker rounds to whole hours first, then divides.
            "hour" => (TimeSpan.FromHours(n) - halfHour, TimeSpan.FromHours(n) + halfHour),
            "day" => (TimeSpan.FromDays(n) - halfHour, TimeSpan.FromDays(n + 1) - halfHour),
            "week" => (TimeSpan.FromDays(7 * n) - halfHour, TimeSpan.FromDays(7 * (n + 1)) - halfHour),
            "month" => (TimeSpan.FromDays(30 * n) - halfHour, TimeSpan.FromDays(30 * (n + 1)) - halfHour),
            // Years truncate the hours instead of rounding them.
            _ => (TimeSpan.FromDays(365 * n), TimeSpan.FromDays(365 * (n + 1))),
        };
    }

    [GeneratedRegex(@"^Up (?<amount>Less than a second|About a minute|About an hour|(?<n>\d+) (?<unit>second|minute|hour|day|week|month|year)s?)\b")]
    private static partial Regex UpText();

    [GeneratedRegex(@"\((?:health: )?(?<health>healthy|unhealthy|starting)\)")]
    private static partial Regex StatusHealth();

    // Docker writes RFC 3339 with nanoseconds, and "0001-01-01T00:00:00Z" for "never". .NET parses at
    // most seven fractional digits, so the last two are dropped first.
    internal static DateTimeOffset? ParseDockerTime(string? value)
    {
        if (value is not null)
        {
            value = ExcessFraction().Replace(value, "$1");
        }

        if (string.IsNullOrEmpty(value) ||
            !DateTimeOffset.TryParse(value, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out var parsed) ||
            parsed.Year <= 1)
        {
            return null;
        }

        return parsed.ToUniversalTime();
    }

    [GeneratedRegex(@"(\.\d{7})\d+")]
    private static partial Regex ExcessFraction();

    private sealed class DockerException(string message) : Exception(message);

    private sealed class ContainerSummary
    {
        public string Id { get; set; } = "";
        public string? State { get; set; }
        // "Up 5 minutes (healthy)", "Exited (0) 3 hours ago", ...
        public string? Status { get; set; }
        public string? Image { get; set; }
        // Unix seconds.
        public long Created { get; set; }
        public Dictionary<string, string>? Labels { get; set; }
        public SummaryHealth? Health { get; set; }
    }

    private sealed class SummaryHealth
    {
        public string? Status { get; set; }
    }
}
