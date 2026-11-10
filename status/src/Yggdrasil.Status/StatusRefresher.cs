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
    HostCatalog catalog,
    StatusOptions options,
    IServiceProvider services,
    SnapshotBuilder builder,
    StatusSnapshotStore store,
    DetailedHealthStore details,
    TimeProvider time,
    ILogger<StatusRefresher> logger) : BackgroundService
{
    // One build at a time, from the liveness loop or from a detailed health fetch. Each reads the
    // newest observations and details inside it, so the later build always carries both; without it,
    // a build that started first and finished last would publish over a newer one. It also keeps
    // LogChanges' fields below to one thread.
    private readonly Lock publishing = new();
    private volatile Round? last;
    private Dictionary<string, StatusLevel> previous = [];
    private Dictionary<string, StatusLevel> previousEnvironments = [];
    private StatusLevel? previousHost;

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

    /// <summary>By <see cref="ProbeTarget.Key"/>, from the last completed refresh; null before the first.</summary>
    public IReadOnlyDictionary<string, Observation>? LastObservations => last?.Observations;

    /// <summary>
    /// Publishes the last observations again, with the details as they are now, so a console that
    /// asked for details doesn't wait for the next tick to see them.
    /// </summary>
    public void Republish()
    {
        lock (publishing)
        {
            if (last is { } round)
            {
                Publish(round);
            }
        }
    }

    public async Task RefreshAsync(CancellationToken cancellationToken)
    {
        // Fresh typed clients every refresh rather than ones captured by this singleton: the factory
        // rotates their handlers, so connections and DNS answers don't outlive a redeployed container
        // that came back on a new address.
        var docker = services.GetRequiredService<DockerClient>();
        var prober = services.GetRequiredService<HealthProber>();

        // Every application in every environment at once, and each platform component once: a refresh
        // takes as long as the slowest probe (at most the 5 s timeout plus Docker's answer), not the sum
        // of them.
        var observations = await Task.WhenAll(catalog.Targets.Select(async target =>
        {
            var found = await docker.FindAsync(target.Container, cancellationToken);

            // Nothing to probe when Docker says there is no container, or that it is stopped in an
            // on-demand environment: the health URL would only fail to resolve, and the console would
            // show that as an error on an application that simply isn't meant to run now.
            var probe = found is DockerObservation.NotFound || StatusRules.IsStopped(found, target.OnDemand)
                ? null
                : await prober.ProbeAsync(target.Health, cancellationToken);

            return (target.Key, Observation: new Observation(found, probe));
        }));

        var round = new Round(observations.ToDictionary(o => o.Key, o => o.Observation), time.GetUtcNow());
        lock (publishing)
        {
            last = round;
            Publish(round);
        }
    }

    // generatedAt stays the time of the observations, also when republishing: it is what tells a
    // reader that refreshes have stopped, so details arriving must not advance it. The caller holds
    // the lock.
    private void Publish(Round round)
    {
        var snapshot = builder.Build(round.Observations, round.At, details.Current);
        store.Set(snapshot);
        LogChanges(snapshot, round.Observations);
    }

    // Transitions only, not every refresh: one line when heimdall-api goes down in production is what
    // someone searching Loki wants; four lines a minute saying it is still up is noise.
    private void LogChanges(HostStatus snapshot, Dictionary<string, Observation> observations)
    {
        // By target key ("heimdall-api.production", "traefik"); a platform component is in every
        // environment's report with the one status, so it is logged once.
        var current = new Dictionary<string, StatusLevel>();
        foreach (var system in snapshot.Systems)
        {
            foreach (var environment in system.Environments)
            {
                foreach (var application in environment.Applications)
                {
                    var key = application.Kind == ApplicationKind.Platform ? application.Id : $"{application.Id}.{environment.Environment}";
                    current[key] = application.Status;
                }
            }
        }

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

        var environments = snapshot.Environments.ToDictionary(e => e.Id, e => e.Status);
        foreach (var (id, status) in environments)
        {
            if (!previousEnvironments.TryGetValue(id, out var before) || before != status)
            {
                logger.LogInformation("Environment {Environment} is {Status}", id, Json.Name(status));
            }
        }

        if (previousHost != snapshot.Status)
        {
            logger.LogInformation("Host {Host} is {Status}", snapshot.Host, Json.Name(snapshot.Status));
        }

        previous = current;
        previousEnvironments = environments;
        previousHost = snapshot.Status;
    }

    private sealed record Round(Dictionary<string, Observation> Observations, DateTimeOffset At);
}
