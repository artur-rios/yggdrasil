using Yggdrasil.Status.Model;

namespace Yggdrasil.Status.Api;

public static class Endpoints
{
    public const string CorsPolicy = "console";

    // How long a client should wait before asking again when the first refresh has not finished.
    // A refresh takes at most the 5 s probe timeout plus Docker's answer.
    private const string RetryAfterSeconds = "5";

    public static void MapStatusApi(this WebApplication app)
    {
        // Answered from memory on both ports, so it says "the process serves HTTP" and nothing about
        // Docker or the applications: a dead Docker proxy must not get this container restarted.
        app.MapGet("/healthz", () => Results.Text("ok"));

        var api = app.MapGroup("/api")
            .AddEndpointFilter<BearerTokenFilter>()
            .RequireCors(CorsPolicy);

        api.MapGet("/status", (StatusSnapshotStore store) =>
            store.Current is { } snapshot ? Results.Ok(snapshot) : NotReady());

        api.MapGet("/systems/{id}", (string id, StatusSnapshotStore store) =>
        {
            if (store.Current is not { } snapshot)
            {
                return NotReady();
            }

            var system = snapshot.Systems.FirstOrDefault(s => string.Equals(s.Id, id, StringComparison.Ordinal));
            return system is null ? Results.NotFound() : Results.Ok(system);
        });

        var internalApi = app.MapGroup("/internal")
            .AddEndpointFilter<InternalPortFilter>();

        // The targets come from the catalog alone, so they are ready before the first refresh and
        // do not change while the process lives.
        internalApi.MapGet("/prometheus/targets", (EnvironmentCatalog catalog) => Results.Ok(PrometheusTargets.From(catalog)));
    }

    // 503 with Retry-After rather than a snapshot of "unknown": the process has not looked yet, and
    // an all-unknown answer would be indistinguishable from "Docker is unreachable", which is a real
    // fault worth showing. This lasts for the first refresh only, a few seconds after start.
    private static IResult NotReady() => new RetryAfterResult();

    private sealed class RetryAfterResult : IResult
    {
        public Task ExecuteAsync(HttpContext httpContext)
        {
            httpContext.Response.StatusCode = StatusCodes.Status503ServiceUnavailable;
            httpContext.Response.Headers.RetryAfter = RetryAfterSeconds;
            return Task.CompletedTask;
        }
    }
}

public static class PrometheusTargets
{
    // This environment's applications only: one that is not deployed here has nothing to scrape, and
    // would sit in Prometheus as a target that is always down.
    public static IReadOnlyList<ScrapeTargetGroup> From(EnvironmentCatalog catalog) =>
        catalog.Systems
            .SelectMany(system => system.Applications
                .Where(application => application.Metrics is not null)
                .Select(application =>
                {
                    var labels = new Dictionary<string, string>
                    {
                        ["system"] = system.Id,
                        ["app"] = application.Id,
                        ["kind"] = KindName(application.Kind),
                    };

                    // Prometheus reads labels starting with "__" as scrape settings; this one replaces
                    // the job's default /metrics for this target only.
                    if (application.MetricsPath is not null)
                    {
                        labels["__metrics_path__"] = application.MetricsPath;
                    }

                    return new ScrapeTargetGroup([application.Metrics!], labels);
                }))
            .ToList();

    private static string KindName(ApplicationKind kind) => kind.ToString().ToLowerInvariant();
}
