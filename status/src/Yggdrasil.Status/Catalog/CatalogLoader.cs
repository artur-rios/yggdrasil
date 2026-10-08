using System.Text.RegularExpressions;
using YamlDotNet.Core;
using YamlDotNet.Serialization;
using YamlDotNet.Serialization.NamingConventions;

namespace Yggdrasil.Status;

/// <summary>
/// Reads catalog.yaml and refuses it whole when anything in it is wrong. The service starts from this
/// file and nothing else, so a typo must stop it at start-up with every problem listed, rather than
/// show up later as an application that is silently never probed.
/// </summary>
public static partial class CatalogLoader
{
    // Ignoring unknown keys is deliberate: the catalog is shared with Jenkins, the GitHub rulesets and
    // deploy.sh, and a field one of them gains must not stop this service. Misspelled required fields
    // are still caught, as missing.
    private static readonly IDeserializer Deserializer = new DeserializerBuilder()
        .WithNamingConvention(CamelCaseNamingConvention.Instance)
        .IgnoreUnmatchedProperties()
        .Build();

    public static Catalog LoadFile(string path)
    {
        if (!File.Exists(path))
        {
            throw new CatalogException($"catalog not found at '{path}' (set YGGDRASIL_CATALOG_PATH or mount it there)");
        }

        return Parse(File.ReadAllText(path), path);
    }

    public static Catalog Parse(string yaml, string source = "catalog.yaml")
    {
        RawCatalog? raw;
        try
        {
            raw = Deserializer.Deserialize<RawCatalog?>(yaml);
        }
        catch (YamlException e)
        {
            // YamlDotNet's own message for a type mismatch is the inner exception; the outer one only
            // carries the position.
            var reason = e.InnerException?.Message ?? e.Message;
            throw new CatalogException($"{source} is not valid YAML at line {e.Start.Line}, column {e.Start.Column}: {reason}");
        }

        var errors = new List<string>();
        var catalog = Validate(raw ?? new RawCatalog(), errors);

        if (errors.Count > 0)
        {
            throw new CatalogException($"{source} is invalid:{Environment.NewLine}" +
                                       string.Join(Environment.NewLine, errors.Select(error => $"  - {error}")));
        }

        return catalog;
    }

    private static Catalog Validate(RawCatalog raw, List<string> errors)
    {
        var owner = raw.Owner?.Trim() ?? "";
        if (owner.Length == 0)
        {
            errors.Add("owner: required (the GitHub account the repositories belong to)");
        }

        var (environments, declared) = ValidateEnvironments(raw.Environments, errors);

        if (raw.Systems is null || raw.Systems.Count == 0)
        {
            errors.Add("systems: at least one system is required");
        }

        var systemIds = new HashSet<string>(StringComparer.Ordinal);
        var applicationIds = new HashSet<string>(StringComparer.Ordinal);
        var systems = new List<SystemDefinition>();

        foreach (var (rawSystem, systemIndex) in (raw.Systems ?? []).Select((system, index) => (system, index)))
        {
            var where = $"systems[{systemIndex}]" + (string.IsNullOrWhiteSpace(rawSystem.Id) ? "" : $" ({rawSystem.Id})");
            var systemId = rawSystem.Id?.Trim() ?? "";

            CheckId(systemId, where, errors);
            if (systemId.Length > 0 && !systemIds.Add(systemId))
            {
                errors.Add($"{where}: duplicate system id '{systemId}'");
            }

            if (string.IsNullOrWhiteSpace(rawSystem.Name))
            {
                errors.Add($"{where}: name is required");
            }

            if (rawSystem.Applications is null || rawSystem.Applications.Count == 0)
            {
                errors.Add($"{where}: at least one application is required");
            }

            var applications = new List<ApplicationDefinition>();
            foreach (var (rawApp, appIndex) in (rawSystem.Applications ?? []).Select((app, index) => (app, index)))
            {
                var application = ValidateApplication(
                    rawApp, $"{where}.applications[{appIndex}]", applicationIds, environments, declared, errors);
                if (application is not null)
                {
                    applications.Add(application);
                }
            }

            systems.Add(new SystemDefinition(systemId, rawSystem.Name?.Trim() ?? "", rawSystem.Description?.Trim() ?? "", applications));
        }

        return new Catalog(owner, environments, systems);
    }

    private static (List<EnvironmentDefinition> Environments, HashSet<string> Declared) ValidateEnvironments(
        List<RawEnvironment>? raw, List<string> errors)
    {
        // At least one: the service reports on the environments YGGDRASIL_ENVIRONMENTS names.
        if (raw is null || raw.Count == 0)
        {
            errors.Add("environments: at least one environment is required");
        }

        var environments = new List<EnvironmentDefinition>();
        var declared = new HashSet<string>(StringComparer.Ordinal);

        foreach (var (rawEnvironment, index) in (raw ?? []).Select((environment, index) => (environment, index)))
        {
            var where = $"environments[{index}]" + (string.IsNullOrWhiteSpace(rawEnvironment.Id) ? "" : $" ({rawEnvironment.Id})");
            var id = rawEnvironment.Id?.Trim() ?? "";

            CheckId(id, where, errors);
            if (id.Length > 0 && !declared.Add(id))
            {
                errors.Add($"{where}: duplicate environment id '{id}'");
            }

            if (string.IsNullOrWhiteSpace(rawEnvironment.Name))
            {
                errors.Add($"{where}: name is required");
            }

            var (hostSuffix, onDemand) = CheckOptions(rawEnvironment, where, errors, isOverride: false);
            environments.Add(new EnvironmentDefinition(id, rawEnvironment.Name?.Trim() ?? "", hostSuffix ?? "", onDemand ?? false));
        }

        return (environments, declared);
    }

    // The deployment options with a closed set of values, on an environment or on an application's
    // override of one: the two this service uses (hostSuffix and onDemand, returned as written, null
    // when not set) and those it only validates; the rest (agent, approval, the timeouts) are left to
    // the tools that read them. The rules are scripts/catalog.py's: a catalog Jenkins and deploy.sh
    // accept must not stop this service.
    private static (string? HostSuffix, bool? OnDemand) CheckOptions(RawOptions options, string where, List<string> errors, bool isOverride)
    {
        var mode = NullIfBlank(options.Mode);
        if (mode is not null and not ("proxy" or "ports"))
        {
            errors.Add($"{where}: mode '{mode}' is not one of proxy, ports");
        }

        var trigger = NullIfBlank(options.Trigger);
        if (trigger is not null and not ("manual" or "branch" or "release"))
        {
            errors.Add($"{where}: trigger '{trigger}' is not one of manual, branch, release");
        }

        if (options.Branches is not null && !IsGlobList(options.Branches))
        {
            errors.Add($"{where}: branches must be a branch glob or a non-empty list of them");
        }

        // Not for an override: one that switches an environment to trigger branch may rely on the
        // environment's branches.
        if (!isOverride && trigger == "branch" && options.Branches is null)
        {
            errors.Add($"{where}: branches is required when trigger is branch (e.g. release/*)");
        }

        // Empty is a value, not "unset": an override of "" takes an environment's suffix away.
        var hostSuffix = options.HostSuffix?.Trim();
        if (hostSuffix is not null && !HostSuffix().IsMatch(hostSuffix))
        {
            errors.Add($"{where}: hostSuffix '{hostSuffix}' may only hold lowercase letters, digits and dashes, " +
                       "and must not end with a dash (e.g. -dev)");
        }

        bool? onDemand = null;
        if (options.OnDemand is not null)
        {
            if (TryParseBool(options.OnDemand, out var value))
            {
                onDemand = value;
            }
            else
            {
                errors.Add($"{where}: onDemand must be true or false");
            }
        }

        return (hostSuffix, onDemand);
    }

    // YAML 1.1's booleans, which PyYAML (catalog.py) and SnakeYAML (Jenkins) read as such: YamlDotNet
    // hands an `object` scalar over as its text.
    private static bool TryParseBool(object value, out bool result)
    {
        (bool ok, result) = value switch
        {
            "true" or "True" or "TRUE" or "yes" or "Yes" or "YES" or "on" or "On" or "ON" => (true, true),
            "false" or "False" or "FALSE" or "no" or "No" or "NO" or "off" or "Off" or "OFF" => (true, false),
            _ => (false, false),
        };
        return ok;
    }

    // A value deserialized as `object` is a string for a scalar and a list for a sequence; anything
    // else (a mapping, a nested list) is a mistake.
    private static bool IsGlobList(object branches) => branches switch
    {
        string glob => !string.IsNullOrWhiteSpace(glob),
        List<object?> globs => globs.Count > 0 && globs.All(glob => glob is string text && !string.IsNullOrWhiteSpace(text)),
        _ => false,
    };

    private static ApplicationDefinition? ValidateApplication(
        RawApplication raw,
        string where,
        HashSet<string> applicationIds,
        List<EnvironmentDefinition> environments,
        HashSet<string> declared,
        List<string> errors)
    {
        var id = raw.Id?.Trim() ?? "";
        if (id.Length > 0)
        {
            where += $" ({id})";
        }

        var errorCount = errors.Count;

        CheckId(id, where, errors);
        // Unique across the whole catalog, not per system: the id is also the Compose project, the
        // network alias and the Prometheus "app" label, all of which live in one namespace per host.
        if (id.Length > 0 && !applicationIds.Add(id))
        {
            errors.Add($"{where}: duplicate application id '{id}'");
        }

        if (string.IsNullOrWhiteSpace(raw.Name))
        {
            errors.Add($"{where}: name is required");
        }

        ApplicationKind kind = default;
        if (string.IsNullOrWhiteSpace(raw.Kind))
        {
            errors.Add($"{where}: kind is required (api, web, worker or platform)");
        }
        else if (!TryParseKind(raw.Kind.Trim(), out kind))
        {
            errors.Add($"{where}: kind '{raw.Kind}' is not one of api, web, worker, platform");
        }

        Uri? health = null;
        if (string.IsNullOrWhiteSpace(raw.Health))
        {
            errors.Add($"{where}: health is required (the URL probed over the Docker networks)");
        }
        else if (!Uri.TryCreate(raw.Health.Trim(), UriKind.Absolute, out health) ||
                 (health.Scheme != Uri.UriSchemeHttp && health.Scheme != Uri.UriSchemeHttps))
        {
            errors.Add($"{where}: health '{raw.Health}' is not an absolute http(s) URL");
        }

        var metrics = NullIfBlank(raw.Metrics);
        if (metrics is not null && !HostPort().IsMatch(metrics))
        {
            errors.Add($"{where}: metrics '{metrics}' is not host:port");
        }

        var metricsPath = NullIfBlank(raw.MetricsPath);
        if (metricsPath is not null)
        {
            if (!metricsPath.StartsWith('/'))
            {
                errors.Add($"{where}: metricsPath '{metricsPath}' must start with /");
            }

            if (metrics is null)
            {
                errors.Add($"{where}: metricsPath is set but metrics is not");
            }
        }

        var host = NullIfBlank(raw.Host);
        if (host is not null && !HostName().IsMatch(host))
        {
            errors.Add($"{where}: host '{host}' is not a host name (lowercase labels, e.g. heimdall-api)");
        }

        var repository = NullIfBlank(raw.Repository);
        if (repository is not null && !RepositoryName().IsMatch(repository))
        {
            errors.Add($"{where}: repository '{repository}' is not a GitHub repository name");
        }

        // "Defaults to id. Omit for platform components": an omitted repository means the id for
        // anything Jenkins deploys, and no repository at all for a platform component.
        repository ??= kind == ApplicationKind.Platform ? null : id;

        var project = NullIfBlank(raw.Container?.Project) ?? id;
        var service = NullIfBlank(raw.Container?.Service);

        var checks = (raw.Checks ?? []).Select(check => check?.Trim() ?? "").ToList();
        if (checks.Any(check => check.Length == 0))
        {
            errors.Add($"{where}: checks has an empty entry");
        }

        // Omitted: every environment. Listed: those, in catalog order whatever order they are written in,
        // each with the options it overrides there.
        var overrides = new Dictionary<string, (string? HostSuffix, bool? OnDemand)>(StringComparer.Ordinal);
        if (raw.Environments is not null)
        {
            if (raw.Environments.Count == 0)
            {
                errors.Add($"{where}: environments lists none (omit it to deploy to every environment)");
            }

            foreach (var (key, options) in raw.Environments)
            {
                if (!declared.Contains(key))
                {
                    errors.Add($"{where}: environments: '{key}' is not an environment " +
                               $"({string.Join(", ", environments.Select(e => e.Id))})");
                }
                else
                {
                    overrides[key] = options is null ? (null, null) : CheckOptions(options, $"{where}.environments.{key}", errors, isOverride: true);
                }
            }
        }

        // Resolved as scripts/catalog.py resolves every option: the default (already in the
        // environment's definition), then the environment's value, then the override.
        var deployments = environments
            .Where(environment => raw.Environments is null || overrides.ContainsKey(environment.Id))
            .Select(environment =>
            {
                var (hostSuffix, onDemand) = overrides.GetValueOrDefault(environment.Id);
                return new ApplicationEnvironment(environment.Id, hostSuffix ?? environment.HostSuffix, onDemand ?? environment.OnDemand);
            })
            .ToList();

        // Where a suffix is appended, <host><hostSuffix> must be one label under DOMAIN, covered by the
        // one *.DOMAIN certificate and DNS record: no dots, and at most DNS's 63 characters. As
        // scripts/catalog.py checks it, in every environment the application deploys to.
        if (host is not null)
        {
            // An invalid suffix is reported as such already.
            foreach (var deployment in deployments.Where(deployment => deployment.HostSuffix.Length > 0 && HostSuffix().IsMatch(deployment.HostSuffix)))
            {
                var name = host + deployment.HostSuffix;
                if (name.Length > MaxLabelLength || !Id().IsMatch(name))
                {
                    errors.Add($"{where}: host '{host}' with the hostSuffix '{deployment.HostSuffix}' of {deployment.Environment} " +
                               $"must be one DNS label of at most {MaxLabelLength} characters (lowercase letters, digits and dashes, no dots)");
                }
            }
        }

        if (errors.Count > errorCount)
        {
            return null;
        }

        return new ApplicationDefinition(
            id, raw.Name!.Trim(), kind, repository, health!, metrics, metricsPath, host, checks,
            new ContainerSelector(project, service), deployments);
    }

    private static void CheckId(string id, string where, List<string> errors)
    {
        if (id.Length == 0)
        {
            errors.Add($"{where}: id is required");
        }
        else if (!Id().IsMatch(id))
        {
            errors.Add($"{where}: id '{id}' may only hold lowercase letters, digits and dashes");
        }
    }

    // Exact lowercase names only: Enum.TryParse would also take "API" or "1".
    private static bool TryParseKind(string value, out ApplicationKind kind)
    {
        (bool ok, kind) = value switch
        {
            "api" => (true, ApplicationKind.Api),
            "web" => (true, ApplicationKind.Web),
            "worker" => (true, ApplicationKind.Worker),
            "platform" => (true, ApplicationKind.Platform),
            _ => (false, default),
        };
        return ok;
    }

    private static string? NullIfBlank(string? value) => string.IsNullOrWhiteSpace(value) ? null : value.Trim();

    private const int MaxLabelLength = 63;

    [GeneratedRegex("^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")]
    private static partial Regex Id();

    // Usually starts with a dash (-dev), so not an id; empty is allowed, and is the default.
    [GeneratedRegex("^([a-z0-9-]*[a-z0-9])?$")]
    private static partial Regex HostSuffix();

    [GeneratedRegex("^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")]
    private static partial Regex HostName();

    [GeneratedRegex("^[A-Za-z0-9.-]+:[0-9]{1,5}$")]
    private static partial Regex HostPort();

    [GeneratedRegex("^[A-Za-z0-9._-]+$")]
    private static partial Regex RepositoryName();

    // The file as written. Everything is optional here so that validation, not the deserializer,
    // reports what is missing -- with the path of the entry it is missing from.
    private sealed class RawCatalog
    {
        public string? Owner { get; set; }
        public List<RawEnvironment>? Environments { get; set; }
        public List<RawSystem>? Systems { get; set; }
    }

    private sealed class RawSystem
    {
        public string? Id { get; set; }
        public string? Name { get; set; }
        public string? Description { get; set; }
        public List<RawApplication>? Applications { get; set; }
    }

    private sealed class RawApplication
    {
        public string? Id { get; set; }
        public string? Name { get; set; }
        public string? Kind { get; set; }
        public string? Repository { get; set; }
        public string? Health { get; set; }
        public string? Metrics { get; set; }
        public string? MetricsPath { get; set; }
        public string? Host { get; set; }
        public List<string?>? Checks { get; set; }
        public RawContainer? Container { get; set; }

        // Environment id -> the options the application overrides there; null or {} overrides none.
        public Dictionary<string, RawOptions?>? Environments { get; set; }
    }

    // Only the options with something to validate: agent, approval, waitTimeout, keepImages and
    // checksTimeout are unmatched properties, like any other key this service doesn't read.
    private class RawOptions
    {
        public string? Mode { get; set; }
        public string? Trigger { get; set; }

        // A glob or a list of globs.
        public object? Branches { get; set; }

        public string? HostSuffix { get; set; }

        // An object rather than a bool, so that a wrong value is one more error in the list rather
        // than a YAML exception that hides every other.
        public object? OnDemand { get; set; }
    }

    private sealed class RawEnvironment : RawOptions
    {
        public string? Id { get; set; }
        public string? Name { get; set; }
    }

    private sealed class RawContainer
    {
        public string? Project { get; set; }
        public string? Service { get; set; }
    }
}

public sealed class CatalogException(string message) : Exception(message);
