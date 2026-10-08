using Yggdrasil.Status.Docker;

namespace Yggdrasil.Status.Model;

/// <summary>
/// The status table of docs/status-api.md, as code and nothing else, so each row can be tested on its
/// own. Everything here is pure: the refresher gathers the observations, this decides.
/// </summary>
public static class StatusRules
{
    /// <summary>A probe slower than this is a degraded application even when it answers 2xx.</summary>
    public static readonly TimeSpan SlowProbe = TimeSpan.FromSeconds(2);

    /// <summary>A restart this recent still counts: a crash loop between two refreshes looks "up".</summary>
    public static readonly TimeSpan RecentRestart = TimeSpan.FromMinutes(10);

    public static StatusLevel ForApplication(DockerObservation docker, ProbeResult? probe, DateTimeOffset now)
    {
        switch (docker)
        {
            case DockerObservation.NotFound:
                return StatusLevel.NotDeployed;

            case DockerObservation.Unreachable:
                // Without Docker, a failing probe is the only evidence there is, and it says down; a
                // passing one cannot say whether the container is healthy, restarting or even the
                // right one, so that is unknown rather than up.
                return probe is { Healthy: true } ? StatusLevel.Unknown : StatusLevel.Down;

            case DockerObservation.Found(var container):
                if (!string.Equals(container.State, "running", StringComparison.OrdinalIgnoreCase))
                {
                    return StatusLevel.Down;
                }

                var probeOk = probe is { Healthy: true } && probe.LatencyMs <= SlowProbe.TotalMilliseconds;
                var healthOk = container.Health is null or "healthy";
                // A restart is this same container started again (restart: unless-stopped after a crash,
                // or the engine restarting); a redeploy creates a new container, so a fresh deploy is
                // not one.
                var restartedRecently = container.Restarted &&
                                        container.StartedAt is { } startedAt &&
                                        now - startedAt < RecentRestart;

                return probeOk && healthOk && !restartedRecently ? StatusLevel.Up : StatusLevel.Degraded;

            default:
                throw new ArgumentOutOfRangeException(nameof(docker), docker, "unknown Docker observation");
        }
    }

    /// <summary>
    /// A system's (or the environment's) status from its applications'. not_deployed is neutral: an
    /// application that does not run on this host -- Jenkins outside production -- neither helps nor
    /// hurts, unless nothing is deployed at all.
    /// </summary>
    public static StatusLevel Aggregate(IEnumerable<StatusLevel> applications)
    {
        var deployed = applications.Where(status => status != StatusLevel.NotDeployed).ToList();

        if (deployed.Count == 0)
        {
            return StatusLevel.NotDeployed;
        }

        // The contract's table and its ranking (down > degraded > unknown > up) agree once "down" is
        // read as the table defines it: the whole system is down only when every deployed application
        // is. One application down beside others that still answer is a degraded system.
        if (deployed.All(status => status == StatusLevel.Down))
        {
            return StatusLevel.Down;
        }

        if (deployed.Any(status => status is StatusLevel.Down or StatusLevel.Degraded))
        {
            return StatusLevel.Degraded;
        }

        if (deployed.Any(status => status == StatusLevel.Unknown))
        {
            return StatusLevel.Unknown;
        }

        return StatusLevel.Up;
    }
}
