namespace Yggdrasil.Status;

/// <summary>
/// The service's settings, all from YGGDRASIL_* environment variables (platform.env on the host).
/// Read once and validated as a whole, so a misconfigured host fails at start-up with every problem
/// named instead of answering with a status nobody can trust.
/// </summary>
public sealed record StatusOptions
{
    public const int PublicPort = 8080;

    // Below this a token is guessable in principle; 32 characters is what `openssl rand -hex 16` or
    // `-base64 24` produce, so it costs nothing to meet.
    public const int MinimumTokenLength = 32;

    public const string EnvironmentsVariable = "YGGDRASIL_ENVIRONMENTS";

    // Before a host could run several environments; still read, as a list of one, so that a host
    // upgraded without editing its platform.env keeps working.
    public const string LegacyEnvironmentVariable = "YGGDRASIL_ENVIRONMENT";

    public required string Token { get; init; }
    /// <summary>The ids of the environments this host runs, as configured (the catalog decides the order).</summary>
    public required IReadOnlyList<string> Environments { get; init; }

    /// <summary>The variable <see cref="Environments"/> came from, to name it in errors.</summary>
    public string EnvironmentsSetting { get; init; } = EnvironmentsVariable;
    public required string Domain { get; init; }
    public required string CatalogPath { get; init; }
    public required Uri DockerUrl { get; init; }
    public required TimeSpan RefreshInterval { get; init; }
    public required int InternalPort { get; init; }
    public required IReadOnlyList<string> CorsOrigins { get; init; }

    public static StatusOptions Load(IConfiguration configuration)
    {
        var errors = new List<string>();

        string Get(string key) => configuration[key]?.Trim() ?? "";

        var token = Get("YGGDRASIL_STATUS_TOKEN");
        if (token.Length == 0)
        {
            errors.Add("YGGDRASIL_STATUS_TOKEN is not set (generate one with `openssl rand -hex 32`)");
        }
        else if (token.Length < MinimumTokenLength)
        {
            errors.Add($"YGGDRASIL_STATUS_TOKEN must be at least {MinimumTokenLength} characters (it has {token.Length})");
        }

        // YGGDRASIL_ENVIRONMENTS wins when both are set. Whether each id is in the catalog is checked
        // once there is a catalog (HostCatalog.For).
        var environmentsSetting = EnvironmentsVariable;
        var environmentsText = Get(EnvironmentsVariable);
        if (environmentsText.Length == 0 && Get(LegacyEnvironmentVariable).Length > 0)
        {
            environmentsSetting = LegacyEnvironmentVariable;
            environmentsText = Get(LegacyEnvironmentVariable);
        }

        var environments = environmentsText
            .Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            .Distinct(StringComparer.Ordinal)
            .ToList();
        if (environments.Count == 0)
        {
            errors.Add($"{EnvironmentsVariable} is not set (the comma-separated ids of the environments in catalog.yaml " +
                       "this host runs, e.g. development,homologation,production)");
        }

        // Only ever used to build https://<host>.<domain>, so a scheme or a trailing dot is a mistake.
        var domain = Get("YGGDRASIL_DOMAIN").TrimEnd('.');
        if (domain.Length == 0)
        {
            errors.Add("YGGDRASIL_DOMAIN is not set (e.g. example.com, or hml.example.com)");
        }
        else if (domain.Contains("://", StringComparison.Ordinal) || domain.Contains('/'))
        {
            errors.Add($"YGGDRASIL_DOMAIN must be a bare domain name, not '{domain}'");
        }

        var catalogPath = Get("YGGDRASIL_CATALOG_PATH");
        if (catalogPath.Length == 0)
        {
            catalogPath = "/app/catalog.yaml";
        }

        var dockerText = Get("YGGDRASIL_DOCKER_URL");
        if (dockerText.Length == 0)
        {
            dockerText = "http://docker-proxy:2375";
        }

        if (!Uri.TryCreate(dockerText, UriKind.Absolute, out var dockerUrl) ||
            (dockerUrl.Scheme != Uri.UriSchemeHttp && dockerUrl.Scheme != Uri.UriSchemeHttps))
        {
            errors.Add($"YGGDRASIL_DOCKER_URL '{dockerText}' is not an absolute http(s) URL");
        }

        var interval = ParseInt(Get("YGGDRASIL_STATUS_INTERVAL_SECONDS"), 15, "YGGDRASIL_STATUS_INTERVAL_SECONDS", 1, 3600, errors);
        var internalPort = ParseInt(Get("YGGDRASIL_STATUS_INTERNAL_PORT"), 8081, "YGGDRASIL_STATUS_INTERNAL_PORT", 1, 65535, errors);
        if (internalPort == PublicPort)
        {
            // The whole point of the internal port is that it is not the routed one.
            errors.Add($"YGGDRASIL_STATUS_INTERNAL_PORT must differ from the public port {PublicPort}");
        }

        var origins = new List<string>();
        foreach (var origin in Get("YGGDRASIL_STATUS_CORS_ORIGINS").Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries))
        {
            // An origin is scheme://host[:port], nothing more; the browser sends exactly that, so a
            // trailing slash or a path here would simply never match.
            if (Uri.TryCreate(origin, UriKind.Absolute, out var uri) &&
                (uri.Scheme == Uri.UriSchemeHttp || uri.Scheme == Uri.UriSchemeHttps) &&
                uri.AbsolutePath == "/" && uri.Query.Length == 0)
            {
                origins.Add(uri.GetLeftPart(UriPartial.Authority));
            }
            else
            {
                errors.Add($"YGGDRASIL_STATUS_CORS_ORIGINS: '{origin}' is not an origin (e.g. https://yggdrasil.example.com)");
            }
        }

        if (errors.Count > 0)
        {
            throw new StartupException("invalid configuration:" + System.Environment.NewLine +
                                       string.Join(System.Environment.NewLine, errors.Select(error => $"  - {error}")));
        }

        return new StatusOptions
        {
            Token = token,
            Environments = environments,
            EnvironmentsSetting = environmentsSetting,
            Domain = domain,
            CatalogPath = catalogPath,
            DockerUrl = dockerUrl!,
            RefreshInterval = TimeSpan.FromSeconds(interval),
            InternalPort = internalPort,
            CorsOrigins = origins,
        };
    }

    private static int ParseInt(string text, int fallback, string name, int min, int max, List<string> errors)
    {
        if (text.Length == 0)
        {
            return fallback;
        }

        if (int.TryParse(text, System.Globalization.NumberStyles.None, System.Globalization.CultureInfo.InvariantCulture, out var value) &&
            value >= min && value <= max)
        {
            return value;
        }

        errors.Add($"{name} must be a whole number from {min} to {max}, not '{text}'");
        return fallback;
    }
}

/// <summary>A configuration or catalog problem that must stop the service before it serves anything.</summary>
public sealed class StartupException(string message) : Exception(message);
