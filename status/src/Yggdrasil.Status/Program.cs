using Yggdrasil.Status;
using Yggdrasil.Status.Api;
using Yggdrasil.Status.Model;
using Yggdrasil.Status.Probing;

// `dotnet Yggdrasil.Status.dll --healthcheck` is the container's health check: the chiseled runtime
// image has no shell and no curl, and this needs neither.
if (args.Contains("--healthcheck"))
{
    return await HealthCheckCommand.RunAsync();
}

var builder = WebApplication.CreateBuilder(args);

// JSON lines on stdout: Alloy ships stdout to Loki, where `| json` then gives every field. Nothing
// is written to files; the container's log driver is the durable copy.
builder.Logging.ClearProviders();
builder.Logging.AddJsonConsole(json =>
{
    json.UseUtcTimestamp = true;
    json.TimestampFormat = "yyyy-MM-dd'T'HH:mm:ss.fff'Z'";
    json.IncludeScopes = false;
});
// HttpClient logs four lines per request at Information: a dozen probes every 15 s would bury
// everything else. Failures are in the snapshot, and transitions are logged by the refresher.
builder.Logging.AddFilter("System.Net.Http.HttpClient", LogLevel.Warning);
builder.Logging.AddFilter("Microsoft.AspNetCore", LogLevel.Warning);
builder.Logging.AddFilter("Microsoft.Hosting.Lifetime", LogLevel.Information);

// Both ports from the same pipeline; what separates them is InternalPortPolicy, not Kestrel. Read
// through the callback's configuration so a bad YGGDRASIL_STATUS_INTERNAL_PORT is reported by
// StatusOptions with the rest, rather than as a Kestrel error.
builder.WebHost.ConfigureKestrel((context, kestrel) =>
{
    kestrel.ListenAnyIP(StatusOptions.PublicPort);
    var internalPort = int.TryParse(context.Configuration["YGGDRASIL_STATUS_INTERNAL_PORT"], out var port) ? port : 8081;
    if (internalPort != StatusOptions.PublicPort && internalPort is > 0 and <= 65535)
    {
        kestrel.ListenAnyIP(internalPort);
    }
});

builder.Services.ConfigureHttpJsonOptions(options => Json.Configure(options.SerializerOptions));

// Resolved lazily, but forced right after Build() below: that is where a bad setting or catalog
// stops the process, and it lets tests substitute either before anything reads them.
builder.Services.AddSingleton(provider => StatusOptions.Load(provider.GetRequiredService<IConfiguration>()));
builder.Services.AddSingleton(provider => CatalogLoader.LoadFile(provider.GetRequiredService<StatusOptions>().CatalogPath));
// What everything else reads: the catalog narrowed to YGGDRASIL_ENVIRONMENTS, each of which must be one
// of its environments. Checked here rather than in StatusOptions, which is read before there is a catalog.
builder.Services.AddSingleton(provider =>
{
    var options = provider.GetRequiredService<StatusOptions>();
    return HostCatalog.For(provider.GetRequiredService<Catalog>(), options.Environments, options.EnvironmentsSetting);
});
builder.Services.AddSingleton(TimeProvider.System);
builder.Services.AddSingleton<StatusSnapshotStore>();
builder.Services.AddSingleton<SnapshotBuilder>();
builder.Services.AddSingleton<InternalPortPolicy>();

builder.Services.AddHttpClient<Yggdrasil.Status.Docker.DockerClient>((provider, http) =>
{
    // Trailing slash so relative paths ("containers/json") append instead of replacing the last segment.
    var baseUrl = provider.GetRequiredService<StatusOptions>().DockerUrl.ToString().TrimEnd('/') + "/";
    http.BaseAddress = new Uri(baseUrl);
    http.Timeout = TimeSpan.FromSeconds(5);
});

builder.Services.AddHttpClient<HealthProber>(http =>
    {
        // HealthProber applies its own 5 s per-probe timeout, to tell timeouts from shutdown.
        http.Timeout = Timeout.InfiniteTimeSpan;
        http.DefaultRequestHeaders.UserAgent.ParseAdd("yggdrasil-status");
    })
    // A health URL must answer itself: following a redirect could take the probe off the Docker
    // networks (to the public host name), and would hide a misconfigured URL behind a 200.
    .ConfigurePrimaryHttpMessageHandler(() => new SocketsHttpHandler { AllowAutoRedirect = false, UseCookies = false });

// A singleton so tests and the host share the one instance; it resolves its (transient) typed
// clients itself on every refresh.
builder.Services.AddSingleton<StatusRefresher>();
builder.Services.AddHostedService(provider => provider.GetRequiredService<StatusRefresher>());

builder.Services.AddCors();
builder.Services.AddOptions<Microsoft.AspNetCore.Cors.Infrastructure.CorsOptions>()
    .Configure<StatusOptions>((cors, status) => cors.AddPolicy(Endpoints.CorsPolicy, policy => policy
        .WithOrigins([.. status.CorsOrigins])
        .WithMethods(HttpMethods.Get)
        .WithHeaders("Authorization")
        // Browsers cap this lower themselves (Chrome at 2 h); it just spares a preflight per poll.
        .SetPreflightMaxAge(TimeSpan.FromHours(1))));

var app = builder.Build();

try
{
    var options = app.Services.GetRequiredService<StatusOptions>();
    var catalog = app.Services.GetRequiredService<HostCatalog>();
    app.Logger.LogInformation(
        "Status API for {Host} ({Environments}): {Targets} applications to probe in {Systems} systems from {CatalogPath}, refreshing every {Interval} s, internal port {InternalPort}",
        options.Domain, string.Join(", ", catalog.Environments.Select(e => e.Id)), catalog.Targets.Count(), catalog.Systems.Count,
        options.CatalogPath, options.RefreshInterval.TotalSeconds, options.InternalPort);
}
catch (Exception e) when (e is StartupException or CatalogException)
{
    // Plain text on stderr, not a JSON log line: this is read by a person running `docker logs`
    // on a container that just exited.
    Console.Error.WriteLine($"yggdrasil-status: {e.Message}");
    return 1;
}

app.UseCors();
app.MapStatusApi();

await app.RunAsync();
return 0;

/// <summary>Visible to WebApplicationFactory in the tests.</summary>
public partial class Program;
