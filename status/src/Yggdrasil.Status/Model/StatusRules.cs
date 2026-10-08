using Yggdrasil.Status.Docker;

namespace Yggdrasil.Status.Model;

/// <summary>
/// The status table and the roll-up rule of docs/status-api.md, as code and nothing else, so each row can be tested on its
/// own. Everything here is pure: the refresher gathers the observations, this decides.
/// </summary>
public static class StatusRules
{
    /// <summary>A probe slower than this is a degraded application even when it answers 2xx.</summary>
    public static readonly TimeSpan SlowProbe = TimeSpan.FromSeconds(2);

    /// <summary>A restart this recent still counts: a crash loop between two refreshes looks "up".</summary>
    public static readonly TimeSpan RecentRestart = TimeSpan.FromMinutes(10);

    /// <summary>
    /// Stopped normally (exited, or created and never started) in an on-demand environment, where that
    /// is how it is meant to be when nobody uses it. Elsewhere the same container is down.
    /// </summary>
    public static bool IsStopped(DockerObservation docker, bool onDemand) =>
        onDemand &&
        docker is DockerObservation.Found(var container) &&
        (string.Equals(container.State, "exited", StringComparison.OrdinalIgnoreCase) ||
         string.Equals(container.State, "created", StringComparison.OrdinalIgnoreCase));

    public static StatusLevel ForApplication(DockerObservation docker, ProbeResult? probe, DateTimeOffset now, bool onDemand = false)
    {
        if (IsStopped(docker, onDemand))
        {
            return StatusLevel.Stopped;
        }

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
                // not one. In an on-demand environment starting a stopped container is how it is used,
                // and the container list can't tell that start from a crash restart, so it doesn't count:
                // a crash loop there still shows as restarting (down) or unhealthy.
                var restartedRecently = !onDemand &&
                                        container.Restarted &&
                                        container.StartedAt is { } startedAt &&
                                        now - startedAt < RecentRestart;

                return probeOk && healthOk && !restartedRecently ? StatusLevel.Up : StatusLevel.Degraded;

            default:
                throw new ArgumentOutOfRangeException(nameof(docker), docker, "unknown Docker observation");
        }
    }

    /// <summary>
    /// The one roll-up rule, at every level: a system's applications in an environment, an environment's
    /// applications, a system's environments, the host's environments and platform components.
    /// not_deployed and stopped are neutral: an application that does not run on this host -- Jenkins
    /// outside production -- or an on-demand environment nobody is using neither helps nor hurts,
    /// unless that is all there is.
    /// </summary>
    public static StatusLevel Aggregate(IEnumerable<StatusLevel> members)
    {
        var deployed = members.Where(status => status != StatusLevel.NotDeployed).ToList();

        if (deployed.Count == 0)
        {
            return StatusLevel.NotDeployed;
        }

        var running = deployed.Where(status => status != StatusLevel.Stopped).ToList();

        if (running.Count == 0)
        {
            return StatusLevel.Stopped;
        }

        // The contract's rule and its ranking (down > degraded > unknown > up) agree once "down" is
        // read as the rule defines it: the whole is down only when every member that should be running
        // is. One member down beside others that still answer is degraded.
        if (running.All(status => status == StatusLevel.Down))
        {
            return StatusLevel.Down;
        }

        if (running.Any(status => status is StatusLevel.Down or StatusLevel.Degraded))
        {
            return StatusLevel.Degraded;
        }

        if (running.Any(status => status == StatusLevel.Unknown))
        {
            return StatusLevel.Unknown;
        }

        return StatusLevel.Up;
    }
}
