namespace Yggdrasil.Status;

/// <summary>
/// catalog.yaml, validated: the environments, every system and the applications each one is made of,
/// in every environment. The service itself reports on the environments of its host: see <see cref="HostCatalog"/>.
/// </summary>
public sealed record Catalog(
    string Owner,
    IReadOnlyList<EnvironmentDefinition> Environments,
    IReadOnlyList<SystemDefinition> Systems)
{
    public IEnumerable<ApplicationDefinition> Applications => Systems.SelectMany(system => system.Applications);
}

/// <summary>
/// An environment as far as this service cares: its own hostSuffix and onDemand, defaulted. Its other
/// deployment options (mode, trigger, approval...) are validated when the catalog is loaded, since the
/// file is refused whole, but used only by Jenkins and deploy.sh.
/// </summary>
public sealed record EnvironmentDefinition(string Id, string Name, string HostSuffix = "", bool OnDemand = false);

public sealed record SystemDefinition(
    string Id,
    string Name,
    string Description,
    IReadOnlyList<ApplicationDefinition> Applications);

public sealed record ApplicationDefinition(
    string Id,
    string Name,
    ApplicationKind Kind,
    // Already defaulted: the id for applications, null for platform components that name none.
    string? Repository,
    Uri Health,
    string? Metrics,
    string? MetricsPath,
    string? Host,
    IReadOnlyList<string> Checks,
    ContainerSelector Container,
    // Already defaulted too: the environments it deploys to, in catalog order, with its options there
    // resolved; every environment when the catalog lists none for it.
    IReadOnlyList<ApplicationEnvironment> Deployments)
{
    public IEnumerable<string> Environments => Deployments.Select(deployment => deployment.Environment);

    /// <summary>Its options in that environment; null when it is not deployed there.</summary>
    public ApplicationEnvironment? In(string environment) =>
        Deployments.FirstOrDefault(deployment => string.Equals(deployment.Environment, environment, StringComparison.Ordinal));
}

/// <summary>
/// An application's options in one environment, resolved as scripts/catalog.py resolves them: the
/// default, then the environment's value, then the application's override.
/// </summary>
public sealed record ApplicationEnvironment(string Environment, string HostSuffix, bool OnDemand);

public enum ApplicationKind
{
    Api,
    Web,
    Worker,
    Platform,
}

/// <summary>
/// How the application's containers are found: by the labels Compose puts on every container it
/// creates. Service is null when the project's containers are all this application's.
/// </summary>
public sealed record ContainerSelector(string Project, string? Service);

/// <summary>
/// The catalog as seen from one host: the environments it runs (YGGDRASIL_ENVIRONMENTS, in catalog
/// order), only the applications deployed to at least one of them, and only the systems that still
/// have one. Everything that reports, probes or exposes targets reads this, so an application that is
/// not meant to run here can't show up as "not deployed" or as a dead target.
/// </summary>
public sealed record HostCatalog(
    IReadOnlyList<EnvironmentDefinition> Environments,
    string Owner,
    IReadOnlyList<SystemDefinition> Systems)
{
    public IEnumerable<ApplicationDefinition> Applications => Systems.SelectMany(system => system.Applications);

    /// <summary>
    /// Every container to look for and health URL to probe, once each: an application in each of the
    /// host's environments it deploys to, and a platform component once for the whole host.
    /// </summary>
    public IEnumerable<ProbeTarget> Targets => Applications.SelectMany(TargetsOf);

    /// <summary>The application's targets: one per host environment it deploys to, or one for a platform component.</summary>
    public IEnumerable<ProbeTarget> TargetsOf(ApplicationDefinition application) => application.Kind == ApplicationKind.Platform
        ? [new ProbeTarget(application, null)]
        : Environments.Where(environment => application.In(environment.Id) is not null)
            .Select(environment => new ProbeTarget(application, environment.Id));

    /// <summary>The system's applications in that environment, in catalog order.</summary>
    public static IEnumerable<ApplicationDefinition> In(SystemDefinition system, EnvironmentDefinition environment) =>
        system.Applications.Where(application => application.In(environment.Id) is not null);

    /// <summary>
    /// Throws <see cref="StartupException"/>, listing every one, when the catalog lacks any of the ids.
    /// <paramref name="setting"/> is the variable they came from, for the message.
    /// </summary>
    public static HostCatalog For(Catalog catalog, IReadOnlyList<string> environmentIds, string setting = "YGGDRASIL_ENVIRONMENTS")
    {
        var unknown = environmentIds
            .Where(id => !catalog.Environments.Any(e => string.Equals(e.Id, id, StringComparison.Ordinal)))
            .Select(id => $"  - {setting}: '{id}' is not an environment in the catalog " +
                          $"({string.Join(", ", catalog.Environments.Select(e => e.Id))})")
            .ToList();
        if (unknown.Count > 0)
        {
            throw new StartupException("invalid configuration:" + System.Environment.NewLine +
                                       string.Join(System.Environment.NewLine, unknown));
        }

        // Catalog order, not the variable's: the console shows them in promotion order either way.
        var environments = catalog.Environments.Where(e => environmentIds.Contains(e.Id, StringComparer.Ordinal)).ToList();

        var systems = catalog.Systems
            .Select(system => system with
            {
                Applications = system.Applications
                    .Where(application => environments.Any(environment => application.In(environment.Id) is not null))
                    .ToList(),
            })
            .Where(system => system.Applications.Count > 0)
            .ToList();

        return new HostCatalog(environments, catalog.Owner, systems);
    }
}

/// <summary>
/// One thing a refresh looks at: an application in one environment, or a platform component (whose
/// Environment is null) once per host. Several environments share the host's Docker engine and
/// networks, so an application's Compose project and network alias carry the environment; a platform
/// component, one per host, keeps its plain names.
/// </summary>
public sealed record ProbeTarget(ApplicationDefinition Application, string? Environment)
{
    /// <summary>What is looked at for the application when reporting on that environment.</summary>
    public static ProbeTarget Of(ApplicationDefinition application, string environment) =>
        new(application, application.Kind == ApplicationKind.Platform ? null : environment);

    /// <summary>"" for a platform component: one per host, under its plain host name.</summary>
    public string HostSuffix => Options?.HostSuffix ?? "";

    /// <summary>Never for a platform component: it serves every environment, and is always meant to run.</summary>
    public bool OnDemand => Options?.OnDemand ?? false;

    /// <summary>Unique per refresh: "heimdall-api.production", or "traefik" for a platform component.</summary>
    public string Key => Qualify(Application.Id, Environment);

    /// <summary>The project deploy.sh names: &lt;container.project or id&gt;-&lt;environment&gt;.</summary>
    public ContainerSelector Container => Environment is null
        ? Application.Container
        : Application.Container with { Project = $"{Application.Container.Project}-{Environment}" };

    /// <summary>The catalog's health URL, reached through the environment's network alias &lt;host&gt;.&lt;environment&gt;.</summary>
    public Uri Health => Environment is null
        ? Application.Health
        : new UriBuilder(Application.Health) { Host = Qualify(Application.Health.Host, Environment) }.Uri;

    /// <summary>The catalog's metrics host:port, its host qualified the same way; null without metrics.</summary>
    public string? Metrics
    {
        get
        {
            if (Application.Metrics is not { } metrics || Environment is null)
            {
                return Application.Metrics;
            }

            var colon = metrics.LastIndexOf(':');
            return $"{Qualify(metrics[..colon], Environment)}{metrics[colon..]}";
        }
    }

    private ApplicationEnvironment? Options => Environment is null ? null : Application.In(Environment);

    private static string Qualify(string name, string? environment) => environment is null ? name : $"{name}.{environment}";
}
