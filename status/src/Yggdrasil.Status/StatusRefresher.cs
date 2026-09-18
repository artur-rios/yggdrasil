using Yggdrasil.Status.Docker;
using Yggdrasil.Status.Model;
using Yggdrasil.Status.Probing;

namespace Yggdrasil.Status;

/// <summary>
/// The only thing that talks to Docker or probes anything. Requests read the snapshot it leaves
/// behind, so a slow or dead application costs the console nothing: no request ever waits on a probe,
/// and a burst of requests can't turn into a burst of probes against the applications.
/// </summary>
public sealed class StatusRefresher(
    Catalog catalog,
    StatusOptions options,
    IServiceProvider services,
    SnapshotBuilder builder,
    StatusSnapshotStore store,
    TimeProvider time,
    ILogger<StatusRefresher> logger) : BackgroundService
{
    private Dictionary<string, StatusLevel> previous = [];
    private StatusLevel? previousEnvironment;

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        // Immediately, not after the first interval: until this completes the API answers 503.
        using var timer = new PeriodicTimer(options.RefreshInterval, time);
        do
        {
            try
            {
                await RefreshAsync(stoppingToken);
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
            {
                return;
            }
            catch (Exception e)
            {
                // Keep the previous snapshot and try again next tick; a status API that stops
                // refreshing for good is worse than one that is a tick late.
                logger.LogError(e, "Status refresh failed");
            }
        }
        while (await timer.WaitForNextTickAsync(stoppingToken));
    }

    public async Task RefreshAsync(CancellationToken cancellationToken)
    {
        // Fresh typed clients every refresh rather than ones captured by this singleton: the factory
        // rotates their handlers, so connections and DNS answers don't outlive a redeployed container
        // that came back on a new address.
        var docker = services.GetRequiredService<DockerClient>();
        var prober = services.GetRequiredService<HealthProber>();

        // Every application at once: a refresh takes as long as the slowest probe (at most the 5 s
        // timeout plus Docker's answer), not the sum of them.
        var observations = await Task.WhenAll(catalog.Applications.Select(async application =>
        {
            var found = await docker.FindAsync(application.Container, cancellationToken);

            // Nothing to probe when Docker says there is no container: the health URL would only fail
            // to resolve, and the console would show that as an error on an application that simply
            // isn't meant to run here.
            var probe = found is DockerObservation.NotFound
                ? null
                : await prober.ProbeAsync(application.Health, cancellationToken);

            return (application.Id, Observation: new Observation(found, probe));
        }));

        var byId = observations.ToDictionary(o => o.Id, o => o.Observation);
        var snapshot = builder.Build(byId, time.GetUtcNow());
        store.Set(snapshot);
        LogChanges(snapshot, byId);
    }

    // Transitions only, not every refresh: one line when heimdall-api goes down is what someone
    // searching Loki wants; four lines a minute saying it is still up is noise.
    private void LogChanges(EnvironmentStatus snapshot, Dictionary<string, Observation> observations)
    {
        var current = snapshot.Systems.SelectMany(s => s.Applications).ToDictionary(a => a.Id, a => a.Status);

        foreach (var (id, status) in current)
        {
            var had = previous.TryGetValue(id, out var before);
            if (had && before == status)
            {
                continue;
            }

            var observation = observations[id];
            var docker = observation.Docker switch
            {
                DockerObservation.Unreachable u => u.Error,
                DockerObservation.Found f => $"container {f.Container.State}{(f.Container.Health is { } h ? $" ({h})" : "")}",
                _ => "no container",
            };
            var probe = observation.Probe switch
            {
                null => "",
                { StatusCode: { } code } p => $"; probe {code} in {p.LatencyMs} ms",
                var p => $"; probe {p.Error} after {p.LatencyMs} ms",
            };

            logger.LogInformation("Application {Application} is {Status} (was {Previous}): {Reason}",
                id, Json.Name(status), had ? Json.Name(before) : "nothing", docker + probe);
        }

        if (previousEnvironment != snapshot.Status)
        {
            logger.LogInformation("Environment {Environment} is {Status}", snapshot.Environment, Json.Name(snapshot.Status));
        }

        previous = current;
        previousEnvironment = snapshot.Status;
    }
}
