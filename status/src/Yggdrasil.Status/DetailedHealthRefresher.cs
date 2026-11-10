using Yggdrasil.Status.Model;
using Yggdrasil.Status.Probing;

namespace Yggdrasil.Status;

/// <summary>
/// Fetches the applications' detailed health reports, only ever because a console asked: there is no
/// loop. Consoles share the work, so several of them or repeated clicks cost the applications one
/// round of calls: requests that arrive during a fetch wait for that fetch, and a result less than
/// <see cref="Reuse"/> old is answered as it is.
/// </summary>
public sealed class DetailedHealthRefresher(
    HostCatalog catalog,
    IServiceProvider services,
    StatusRefresher status,
    DetailedHealthStore store,
    TimeProvider time,
    IHostApplicationLifetime lifetime)
{
    private static readonly TimeSpan Reuse = TimeSpan.FromSeconds(10);

    private readonly Lock gate = new();
    private Task? running;
    private DateTimeOffset? finishedAt;

    /// <summary>
    /// Completes when the details are as fresh as this call needs, having merged them into the
    /// snapshot. Takes no token: the fetch belongs to everyone waiting on it, so one client going
    /// away must not cancel it for the others.
    /// </summary>
    public Task RefreshAsync()
    {
        lock (gate)
        {
            if (running is not null)
            {
                return running;
            }

            if (finishedAt is { } finished && time.GetUtcNow() - finished < Reuse)
            {
                return Task.CompletedTask;
            }

            // On the thread pool so nothing of it runs here: the fetch clears this slot when it ends,
            // and a fetch that ended inside this call would be overwritten by the assignment and
            // stay in the slot for good.
            running = Task.Run(FetchAsync);
            return running;
        }
    }

    private async Task FetchAsync()
    {
        var succeeded = false;
        try
        {
            var observations = status.LastObservations
                ?? throw new InvalidOperationException("Detailed health fetched before the first liveness refresh");

            // A fresh typed client each time, for the reason StatusRefresher gives.
            var client = services.GetRequiredService<DetailedHealthClient>();
            var now = time.GetUtcNow();

            // All at once, so this takes as long as the slowest report (at most its 5 s timeout).
            var results = await Task.WhenAll(catalog.Targets
                .Where(target => target.DetailedHealth is not null && IsMeantToAnswer(target, observations[target.Key], now))
                .Select(async target =>
                    (target.Key, Details: await client.FetchAsync(target.DetailedHealth!, lifetime.ApplicationStopping))));

            // Before republishing: a liveness build that starts after this point has them too.
            store.Set(results.ToDictionary(result => result.Key, result => result.Details));
            status.Republish();
            succeeded = true;
        }
        finally
        {
            // Together, so no request can find the slot empty and the previous result still current.
            // A fault has no result to reuse, so the next request tries again at once.
            lock (gate)
            {
                running = null;
                if (succeeded)
                {
                    finishedAt = time.GetUtcNow();
                }
            }
        }
    }

    private static bool IsMeantToAnswer(ProbeTarget target, Observation observation, DateTimeOffset now) =>
        StatusRules.ForApplication(observation.Docker, observation.Probe, now, target.OnDemand)
            is not (StatusLevel.Stopped or StatusLevel.NotDeployed);
}

/// <summary>
/// The result of the last detailed health fetch, by <see cref="ProbeTarget.Key"/>. Immutable and
/// replaced whole, like <see cref="StatusSnapshotStore"/>, so a reference swap is all the
/// synchronisation the liveness loop needs to read it on every refresh.
/// </summary>
public sealed class DetailedHealthStore
{
    private volatile IReadOnlyDictionary<string, ApplicationDetails> current = new Dictionary<string, ApplicationDetails>();

    /// <summary>Empty until the first fetch; only the targets that last fetch reached after that.</summary>
    public IReadOnlyDictionary<string, ApplicationDetails> Current => current;

    public void Set(IReadOnlyDictionary<string, ApplicationDetails> details) => current = details;
}
