using System.Net;
using System.Text.Json;
using System.Text.RegularExpressions;
using Yggdrasil.Status.Model;

namespace Yggdrasil.Status.Probing;

/// <summary>
/// One GET to an application's detailed health report (docs/status-api.md). Never throws: every
/// way it can go wrong is a state. The report is the application's own words, shown on a page, so
/// nothing is passed on unless it matches the contract: an unknown status rejects the whole report,
/// and a reason outside the probe vocabulary is dropped (it may carry a connection string).
/// </summary>
public sealed partial class DetailedHealthClient(HttpClient http, TimeProvider time)
{
    public const int MaxBodyBytes = 64 * 1024;

    private static readonly TimeSpan Timeout = TimeSpan.FromSeconds(5);

    private static readonly HashSet<string> Statuses = ["Healthy", "Degraded", "Unhealthy", "NotConfigured"];

    // HealthProber.Describe's vocabulary, plus "timeout"; "HTTP <code>" is matched separately.
    private static readonly HashSet<string> Reasons =
    [
        "timeout", "connection refused", "connection failed", "connection reset", "connection closed",
        "name not resolved", "host unreachable", "tls error", "invalid response", "request failed",
    ];

    public const int MaxServices = 50;

    // Names are shown as is, so they are held to a plain label: no "=" or ";" that could carry a
    // connection string, no control characters.
    [GeneratedRegex(@"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}\z")]
    private static partial Regex ServiceName();

    [GeneratedRegex(@"^HTTP [0-9]{3}\z")]
    private static partial Regex HttpReason();

    public async Task<ApplicationDetails> FetchAsync(Uri url, CancellationToken cancellationToken)
    {
        // The timeout is per fetch rather than HttpClient.Timeout so a timeout can be told apart
        // from the service shutting down.
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        timeout.CancelAfter(Timeout);

        try
        {
            using var response = await http.GetAsync(url, timeout.Token);
            if (response.StatusCode == HttpStatusCode.NotFound)
            {
                return Without(DetailsStates.Unsupported, null);
            }

            // 503 is the report of an unhealthy application, not a failure to report.
            var code = (int)response.StatusCode;
            if (code is not (200 or 503))
            {
                return Without(DetailsStates.Unavailable, $"HTTP {code}");
            }

            var body = await response.Content.ReadAsStringAsync(timeout.Token);
            return Parse(body) ?? Without(DetailsStates.Unavailable, "invalid response");
        }
        catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
        {
            return Without(DetailsStates.Unavailable, "timeout");
        }
        catch (HttpRequestException e)
        {
            return Without(DetailsStates.Unavailable, HealthProber.Describe(e));
        }
    }

    private ApplicationDetails Without(string state, string? reason) => new(state, time.GetUtcNow(), reason, null, []);

    private ApplicationDetails? Parse(string body)
    {
        try
        {
            using var document = JsonDocument.Parse(body);
            var root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object
                || !TryStatus(root, out var status)
                || !root.TryGetProperty("services", out var list)
                || list.ValueKind != JsonValueKind.Array)
            {
                return null;
            }

            var services = new List<ReportedService>();
            foreach (var entry in list.EnumerateArray())
            {
                if (entry.ValueKind != JsonValueKind.Object
                    || !entry.TryGetProperty("name", out var name)
                    || name.ValueKind != JsonValueKind.String
                    || name.GetString() is not { } text
                    || !ServiceName().IsMatch(text)
                    || services.Count >= MaxServices
                    || !TryStatus(entry, out var entryStatus))
                {
                    return null;
                }

                services.Add(new ReportedService(text, entryStatus, Reason(entry)));
            }

            return new ApplicationDetails(DetailsStates.Reported, time.GetUtcNow(), null, status, services);
        }
        catch (JsonException)
        {
            return null;
        }
    }

    private static bool TryStatus(JsonElement element, out string status)
    {
        status = "";
        if (element.TryGetProperty("status", out var value) && value.ValueKind == JsonValueKind.String
            && value.GetString() is { } word && Statuses.Contains(word))
        {
            status = word;
            return true;
        }

        return false;
    }

    private static string? Reason(JsonElement entry) =>
        entry.TryGetProperty("reason", out var value) && value.ValueKind == JsonValueKind.String
        && value.GetString() is { } reason && (Reasons.Contains(reason) || HttpReason().IsMatch(reason))
            ? reason
            : null;
}
