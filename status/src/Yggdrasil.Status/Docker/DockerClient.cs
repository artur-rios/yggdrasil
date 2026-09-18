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
    DateTimeOffset? StartedAt,
    int RestartCount,
    string? Image,
    IReadOnlyDictionary<string, string> Labels);

/// <summary>
/// Docker Engine API over HTTP, through tecnativa/docker-socket-proxy with CONTAINERS=1 and nothing
/// else. The proxy is the reason this is plain HTTP and not the socket: the status API is reachable
/// from the edge network, so it must not hold a socket that can start containers -- the proxy only
/// lets GETs on /containers through.
/// </summary>
public sealed partial class DockerClient(HttpClient http)
{
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

            if (chosen is null)
            {
                return new DockerObservation.NotFound();
            }

            // The list has no health, start time or restart count; those are only on the inspect.
            var inspected = await GetAsync<ContainerInspect>($"containers/{Uri.EscapeDataString(chosen.Id)}/json", cancellationToken);
            if (inspected is null)
            {
                // Removed between the two calls, which is what a redeploy looks like from here.
                return new DockerObservation.NotFound();
            }

            return new DockerObservation.Found(ToDetails(inspected, chosen));
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
            // 403 is the proxy refusing: CONTAINERS=1 is missing from its environment.
            throw new DockerException($"docker proxy answered {(int)response.StatusCode}");
        }

        await using var body = await response.Content.ReadAsStreamAsync(cancellationToken);
        return await JsonSerializer.DeserializeAsync<T>(body, JsonOptions, cancellationToken)
               ?? throw new JsonException("empty body");
    }

    private static ContainerDetails ToDetails(ContainerInspect inspect, ContainerSummary summary)
    {
        var health = inspect.State?.Health?.Status;
        // "none" is what Docker reports for a container whose image has no health check.
        if (string.IsNullOrEmpty(health) || health == "none")
        {
            health = null;
        }

        return new ContainerDetails(
            inspect.Id ?? summary.Id,
            inspect.State?.Status ?? summary.State ?? "unknown",
            health,
            ParseDockerTime(inspect.State?.StartedAt),
            inspect.RestartCount,
            inspect.Config?.Image ?? summary.Image,
            inspect.Config?.Labels ?? summary.Labels ?? new Dictionary<string, string>());
    }

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
        public string? Image { get; set; }
        public long Created { get; set; }
        public Dictionary<string, string>? Labels { get; set; }
    }

    private sealed class ContainerInspect
    {
        public string? Id { get; set; }
        public int RestartCount { get; set; }
        public InspectState? State { get; set; }
        public InspectConfig? Config { get; set; }
    }

    private sealed class InspectState
    {
        public string? Status { get; set; }
        public string? StartedAt { get; set; }
        public InspectHealth? Health { get; set; }
    }

    private sealed class InspectHealth
    {
        public string? Status { get; set; }
    }

    private sealed class InspectConfig
    {
        public string? Image { get; set; }
        public Dictionary<string, string>? Labels { get; set; }
    }
}
