namespace Yggdrasil.Status.Tests;

public class CatalogLoaderTests
{
    [Fact]
    public void GivenTheRepositoryCatalog_WhenLoaded_ThenEverySystemAndApplicationIsRead()
    {
        var catalog = CatalogLoader.LoadFile(Path.Combine(AppContext.BaseDirectory, "catalog.yaml"));

        Assert.Equal("artur-rios", catalog.Owner);
        Assert.Equal(["development", "homologation", "production"], catalog.Environments.Select(e => e.Id));
        Assert.All(catalog.Environments, e => Assert.NotEmpty(e.Name));
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
        Assert.Equal(["development", "homologation", "production"], api.Environments);

        Assert.Equal(
            [new EnvironmentDefinition("development", "Development"), new EnvironmentDefinition("homologation", "Homologation"),
             new EnvironmentDefinition("production", "Production")],
            catalog.Environments);

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
        Assert.Contains("environments: at least one environment is required", error.Message);
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
            environments: [{ id: env, name: Env }]
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
            environments: [{ id: env, name: Env }]
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

    [Fact]
    public void GivenAnApplicationWithEnvironments_WhenParsed_ThenItDeploysToThoseInCatalogOrder()
    {
        var catalog = CatalogLoader.Parse(Minimal(
            "kind: api\n        environments:\n          prod: { approval: true }\n          dev:",
            environments: "- { id: dev, name: Dev }\n  - { id: hml, name: Hml }\n  - { id: prod, name: Prod }"));

        Assert.Equal(["dev", "prod"], catalog.Applications.Single().Environments);
    }

    [Fact]
    public void GivenEveryEnvironmentOption_WhenParsed_ThenTheOnesThisServiceDoesNotReadAreIgnored()
    {
        var catalog = CatalogLoader.Parse(Minimal("kind: api",
            environments: "- { id: dev, name: Dev, mode: ports, trigger: branch, branches: [develop, hotfix/*], agent: laptop, " +
                          "approval: yes, waitTimeout: 600, keepImages: 5, checksTimeout: 3600, somethingNew: { a: 1 } }"));

        Assert.Equal(new EnvironmentDefinition("dev", "Dev"), catalog.Environments.Single());
    }

    [Theory]
    [InlineData("- { id: dev, name: Dev, mode: docker }", "environments[0] (dev): mode 'docker' is not one of proxy, ports")]
    [InlineData("- { id: dev, name: Dev, trigger: nightly }", "environments[0] (dev): trigger 'nightly' is not one of manual, branch, release")]
    [InlineData("- { id: dev, name: Dev, trigger: branch }", "environments[0] (dev): branches is required when trigger is branch")]
    [InlineData("- { id: dev, name: Dev, trigger: branch, branches: [] }", "environments[0] (dev): branches must be a branch glob or a non-empty list")]
    [InlineData("- { id: dev, name: Dev, branches: { a: b } }", "environments[0] (dev): branches must be a branch glob")]
    [InlineData("- { id: dev }", "environments[0] (dev): name is required")]
    [InlineData("- { name: Dev }", "environments[0]: id is required")]
    [InlineData("- { id: Dev_1, name: Dev }", "id 'Dev_1' may only hold lowercase letters, digits and dashes")]
    [InlineData("- { id: dev, name: Dev }\n  - { id: dev, name: Dev again }", "environments[1] (dev): duplicate environment id 'dev'")]
    public void GivenAnInvalidEnvironment_WhenParsed_ThenTheErrorNamesIt(string environments, string expected)
    {
        var error = Assert.Throws<CatalogException>(() => CatalogLoader.Parse(Minimal("kind: api", environments: environments)));

        Assert.Contains(expected, error.Message);
    }

    [Theory]
    [InlineData("environments: { staging: {} }", "applications[0] (app): environments: 'staging' is not an environment (dev, rel)")]
    [InlineData("environments: {}", "applications[0] (app): environments lists none")]
    [InlineData("environments: { dev: { mode: docker } }", "applications[0] (app).environments.dev: mode 'docker' is not one of proxy, ports")]
    [InlineData("environments: { dev: { trigger: weekly } }", "applications[0] (app).environments.dev: trigger 'weekly' is not one of")]
    public void GivenAnInvalidApplicationEnvironment_WhenParsed_ThenTheErrorNamesIt(string field, string expected)
    {
        var yaml = Minimal("kind: api\n        " + field,
            environments: "- { id: dev, name: Dev, trigger: branch, branches: develop }\n  - { id: rel, name: Rel, trigger: release }");

        var error = Assert.Throws<CatalogException>(() => CatalogLoader.Parse(yaml));

        Assert.Contains(expected, error.Message);
    }

    [Fact]
    public void GivenAnOverrideSwitchingToTriggerBranch_WhenParsed_ThenItNeedsNoBranchesOfItsOwn()
    {
        // As scripts/catalog.py has it: the environment's branches, if any, apply.
        var catalog = CatalogLoader.Parse(Minimal("kind: api\n        environments: { dev: { trigger: branch } }",
            environments: "- { id: dev, name: Dev, trigger: manual }"));

        Assert.Equal(["dev"], catalog.Applications.Single().Environments);
    }

    [Fact]
    public void GivenAnEnvironmentId_WhenNarrowingTheCatalogToIt_ThenOnlyItsApplicationsAndTheirSystemsRemain()
    {
        var production = TestData.InEnvironment("production");
        var development = TestData.InEnvironment("development");

        Assert.Equal(new EnvironmentDefinition("production", "Production"), production.Environment);
        Assert.Equal("artur-rios", production.Owner);
        // heimdall-worker is not deployed to production, and sandbox-api, the only application of
        // sandbox, is not either: the system goes with it.
        Assert.Equal(["heimdall", "yggdrasil"], production.Systems.Select(s => s.Id));
        Assert.Equal(["heimdall-api", "heimdall-ui", "traefik", "jenkins"], production.Applications.Select(a => a.Id));

        Assert.Equal(["heimdall", "yggdrasil", "sandbox"], development.Systems.Select(s => s.Id));
        Assert.Contains(development.Applications, a => a.Id == "heimdall-worker");
    }

    [Fact]
    public void GivenAnEnvironmentIdNotInTheCatalog_WhenNarrowingTheCatalogToIt_ThenStartupIsRefused()
    {
        var error = Assert.Throws<StartupException>(() => EnvironmentCatalog.For(TestData.Catalog(), "staging"));

        Assert.Contains("YGGDRASIL_ENVIRONMENT 'staging' is not an environment in the catalog (development, homologation, production)", error.Message);
    }

    // One system "sys" with one application "app"; `fields` supplies kind and anything else, each
    // further line indented by eight spaces to stay inside the application. `environments` is the
    // top-level list, one flow mapping per line.
    private static string Minimal(string fields, bool withHealth = true, string environments = "- { id: dev, name: Dev }") => $"""
        owner: o
        environments:
          {environments}
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
