namespace Yggdrasil.Status.Tests;

public class CatalogLoaderTests
{
    [Fact]
    public void GivenTheRepositoryCatalog_WhenLoaded_ThenEverySystemAndApplicationIsRead()
    {
        var catalog = CatalogLoader.LoadFile(Path.Combine(AppContext.BaseDirectory, "catalog.yaml"));

        Assert.Equal("artur-rios", catalog.Owner);
        Assert.Equal(["heimdall", "fortuna", "yggdrasil"], catalog.Systems.Select(s => s.Id));
        Assert.Contains(catalog.Applications, a => a.Id == "jenkins" && a.MetricsPath == "/prometheus/");
    }

    [Fact]
    public void GivenEveryField_WhenParsed_ThenEachIsMapped()
    {
        var catalog = TestData.Catalog();

        var heimdall = catalog.Systems[0];
        Assert.Equal(("heimdall", "Heimdall", "Identity and access management"), (heimdall.Id, heimdall.Name, heimdall.Description));

        var api = heimdall.Applications[0];
        Assert.Equal("heimdall-api", api.Id);
        Assert.Equal("Heimdall API", api.Name);
        Assert.Equal(ApplicationKind.Api, api.Kind);
        Assert.Equal("heimdall-api", api.Repository);
        Assert.Equal(new Uri("http://heimdall-api:8080/healthcheck"), api.Health);
        Assert.Equal("heimdall-api:9464", api.Metrics);
        Assert.Null(api.MetricsPath);
        Assert.Equal("heimdall-api", api.Host);
        Assert.Equal(["test", "docker"], api.Checks);
        Assert.Equal(new ContainerSelector("heimdall-api", null), api.Container);

        var jenkins = catalog.Systems[1].Applications[1];
        Assert.Equal(ApplicationKind.Platform, jenkins.Kind);
        Assert.Null(jenkins.Repository);
        Assert.Equal("/prometheus/", jenkins.MetricsPath);
        Assert.Equal(new ContainerSelector("yggdrasil", "jenkins"), jenkins.Container);
    }

    [Fact]
    public void GivenAnExplicitRepository_WhenParsed_ThenItWinsOverTheDefault()
    {
        var catalog = CatalogLoader.Parse(Minimal("repository: other-repo\n        kind: platform"));

        Assert.Equal("other-repo", catalog.Applications.Single().Repository);
    }

    [Theory]
    [InlineData("kind: service", "kind 'service' is not one of api, web, worker, platform")]
    [InlineData("kind: API", "kind 'API' is not one of")]
    [InlineData("kind: api\n        health: /healthz", "is not an absolute http(s) URL", false)]
    [InlineData("kind: api\n        health: ftp://x/healthz", "is not an absolute http(s) URL", false)]
    [InlineData("kind: api\n        metrics: heimdall-api", "is not host:port")]
    [InlineData("kind: api\n        metrics: a:1\n        metricsPath: prometheus", "must start with /")]
    [InlineData("kind: api\n        metricsPath: /prometheus", "metricsPath is set but metrics is not")]
    [InlineData("kind: api\n        host: https://x", "is not a host name")]
    public void GivenAnInvalidField_WhenParsed_ThenTheErrorNamesIt(string fields, string expected, bool withHealth = true)
    {
        var yaml = Minimal(fields, withHealth);

        var error = Assert.Throws<CatalogException>(() => CatalogLoader.Parse(yaml));

        Assert.Contains("systems[0] (sys).applications[0] (app)", error.Message);
        Assert.Contains(expected, error.Message);
    }

    [Fact]
    public void GivenMissingRequiredFields_WhenParsed_ThenEveryProblemIsListedAtOnce()
    {
        const string yaml = """
            systems:
              - id: sys
                applications:
                  - id: Bad_Id
            """;

        var error = Assert.Throws<CatalogException>(() => CatalogLoader.Parse(yaml));

        Assert.Contains("owner: required", error.Message);
        Assert.Contains("systems[0] (sys): name is required", error.Message);
        Assert.Contains("id 'Bad_Id' may only hold lowercase letters, digits and dashes", error.Message);
        Assert.Contains("kind is required", error.Message);
        Assert.Contains("health is required", error.Message);
    }

    [Fact]
    public void GivenDuplicateApplicationIdsAcrossSystems_WhenParsed_ThenRejected()
    {
        const string yaml = """
            owner: o
            systems:
              - id: a
                name: A
                applications:
                  - { id: app, name: App, kind: api, health: "http://app/h" }
              - id: b
                name: B
                applications:
                  - { id: app, name: App, kind: api, health: "http://app/h" }
            """;

        var error = Assert.Throws<CatalogException>(() => CatalogLoader.Parse(yaml));

        Assert.Contains("duplicate application id 'app'", error.Message);
    }

    [Fact]
    public void GivenDuplicateSystemIds_WhenParsed_ThenRejected()
    {
        const string yaml = """
            owner: o
            systems:
              - id: a
                name: A
                applications:
                  - { id: one, name: One, kind: api, health: "http://one/h" }
              - id: a
                name: A again
                applications:
                  - { id: two, name: Two, kind: api, health: "http://two/h" }
            """;

        var error = Assert.Throws<CatalogException>(() => CatalogLoader.Parse(yaml));

        Assert.Contains("duplicate system id 'a'", error.Message);
    }

    [Fact]
    public void GivenAnEmptyCatalog_WhenParsed_ThenRejected()
    {
        var error = Assert.Throws<CatalogException>(() => CatalogLoader.Parse(""));

        Assert.Contains("at least one system is required", error.Message);
    }

    [Fact]
    public void GivenMalformedYaml_WhenParsed_ThenTheErrorHasTheLine()
    {
        var error = Assert.Throws<CatalogException>(() => CatalogLoader.Parse("owner: o\nsystems: [\n  - id: x"));

        Assert.Contains("is not valid YAML at line", error.Message);
    }

    [Fact]
    public void GivenUnknownKeys_WhenParsed_ThenTheyAreIgnored()
    {
        var catalog = CatalogLoader.Parse(Minimal("kind: api\n        somethingNew: true"));

        Assert.Single(catalog.Applications);
    }

    [Fact]
    public void GivenAMissingFile_WhenLoaded_ThenTheErrorSaysWhere()
    {
        var error = Assert.Throws<CatalogException>(() => CatalogLoader.LoadFile("/nowhere/catalog.yaml"));

        Assert.Contains("/nowhere/catalog.yaml", error.Message);
    }

    // One system "sys" with one application "app"; `fields` supplies kind and anything else, each
    // further line indented by eight spaces to stay inside the application.
    private static string Minimal(string fields, bool withHealth = true) => $"""
        owner: o
        systems:
          - id: sys
            name: System
            applications:
              - id: app
                name: App
                {(withHealth ? "health: http://app:8080/healthz" : "")}
                {fields}
        """;
}
