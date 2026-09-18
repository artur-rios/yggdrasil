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
                var application = ValidateApplication(rawApp, $"{where}.applications[{appIndex}]", applicationIds, errors);
                if (application is not null)
                {
                    applications.Add(application);
                }
            }

            systems.Add(new SystemDefinition(systemId, rawSystem.Name?.Trim() ?? "", rawSystem.Description?.Trim() ?? "", applications));
        }

        return new Catalog(owner, systems);
    }

    private static ApplicationDefinition? ValidateApplication(
        RawApplication raw, string where, HashSet<string> applicationIds, List<string> errors)
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

        if (errors.Count > errorCount)
        {
            return null;
        }

        return new ApplicationDefinition(
            id, raw.Name!.Trim(), kind, repository, health!, metrics, metricsPath, host, checks,
            new ContainerSelector(project, service));
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

    [GeneratedRegex("^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")]
    private static partial Regex Id();

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
    }

    private sealed class RawContainer
    {
        public string? Project { get; set; }
        public string? Service { get; set; }
    }
}

public sealed class CatalogException(string message) : Exception(message);
