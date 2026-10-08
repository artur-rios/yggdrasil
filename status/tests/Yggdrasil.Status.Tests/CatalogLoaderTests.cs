namespace Yggdrasil.Status.Tests;

public class CatalogLoaderTests
{
    // Only that it loads, and the platform it ships with: the systems and environments are the
    // installation's own, and every other test uses TestData's catalog.
    [Fact]
    public void GivenTheRepositoryCatalog_WhenLoaded_ThenItIsValid()
    {
        var catalog = CatalogLoader.LoadFile(Path.Combine(AppContext.BaseDirectory, "catalog.yaml"));

        Assert.NotEmpty(catalog.Owner);
        Assert.NotEmpty(catalog.Environments);
        Assert.All(catalog.Environments, e => Assert.NotEmpty(e.Name));
        Assert.Contains(catalog.Applications, a => a.Id == "jenkins" && a.Kind == ApplicationKind.Platform && a.MetricsPath == "/prometheus/");
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
        Assert.Equal(["local", "development", "homologation", "production"], api.Environments);
        Assert.Equal(
            [new ApplicationEnvironment("local", "", false), new ApplicationEnvironment("development", "-dev", true),
             new ApplicationEnvironment("homologation", "-hml", true), new ApplicationEnvironment("production", "", false)],
            api.Deployments);

        Assert.Equal(
            [new EnvironmentDefinition("local", "Local"), new EnvironmentDefinition("development", "Development", "-dev", true),
             new EnvironmentDefinition("homologation", "Homologation", "-hml", true), new EnvironmentDefinition("production", "Production")],
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
    [InlineData("hostSuffix: -dev, onDemand: true", "-dev", true)]
    [InlineData("hostSuffix: '', onDemand: false", "", false)]
    [InlineData("hostSuffix: x2, onDemand: yes", "x2", true)]
    [InlineData("hostSuffix: -a-1, onDemand: off", "-a-1", false)]
    [InlineData("hostSuffix: , onDemand: ", "", false)]
    public void GivenHostSuffixAndOnDemand_WhenParsed_ThenTheEnvironmentHasThem(string options, string hostSuffix, bool onDemand)
    {
        var catalog = CatalogLoader.Parse(Minimal("kind: api", environments: $"- {{ id: dev, name: Dev, {options} }}"));

        Assert.Equal(new EnvironmentDefinition("dev", "Dev", hostSuffix, onDemand), catalog.Environments.Single());
        Assert.Equal(new ApplicationEnvironment("dev", hostSuffix, onDemand), catalog.Applications.Single().Deployments.Single());
    }

    [Fact]
    public void GivenApplicationOverrides_WhenParsed_ThenEachOptionResolvesDefaultThenEnvironmentThenOverride()
    {
        var catalog = CatalogLoader.Parse(Minimal(
            "kind: api\n        environments:\n" +
            "          dev: { hostSuffix: '' }\n" +
            "          hml: { onDemand: false, hostSuffix: -staging }\n" +
            "          prod: { onDemand: true }\n" +
            "          other:",
            environments: "- { id: dev, name: Dev, hostSuffix: -dev, onDemand: true }\n" +
                          "  - { id: hml, name: Hml, hostSuffix: -hml, onDemand: true }\n" +
                          "  - { id: prod, name: Prod }\n" +
                          "  - { id: other, name: Other, hostSuffix: -o }"));

        Assert.Equal(
            [
                new ApplicationEnvironment("dev", "", true),
                new ApplicationEnvironment("hml", "-staging", false),
                new ApplicationEnvironment("prod", "", true),
                new ApplicationEnvironment("other", "-o", false),
            ],
            catalog.Applications.Single().Deployments);
        Assert.Equal(new ApplicationEnvironment("hml", "-staging", false), catalog.Applications.Single().In("hml"));
        Assert.Null(catalog.Applications.Single().In("nope"));
    }

    [Theory]
    [InlineData("- { id: dev, name: Dev, hostSuffix: -Dev }", "environments[0] (dev): hostSuffix '-Dev' may only hold lowercase letters, digits and dashes")]
    [InlineData("- { id: dev, name: Dev, hostSuffix: dev- }", "environments[0] (dev): hostSuffix 'dev-' may only hold")]
    [InlineData("- { id: dev, name: Dev, hostSuffix: '-' }", "environments[0] (dev): hostSuffix '-' may only hold")]
    [InlineData("- { id: dev, name: Dev, hostSuffix: .dev }", "environments[0] (dev): hostSuffix '.dev' may only hold")]
    [InlineData("- { id: dev, name: Dev, hostSuffix: -d_v }", "environments[0] (dev): hostSuffix '-d_v' may only hold")]
    [InlineData("- { id: dev, name: Dev, onDemand: sometimes }", "environments[0] (dev): onDemand must be true or false")]
    [InlineData("- { id: dev, name: Dev, onDemand: 1 }", "environments[0] (dev): onDemand must be true or false")]
    [InlineData("- { id: dev, name: Dev, onDemand: [true] }", "environments[0] (dev): onDemand must be true or false")]
    public void GivenAnInvalidHostSuffixOrOnDemand_WhenParsed_ThenTheErrorNamesIt(string environments, string expected)
    {
        var error = Assert.Throws<CatalogException>(() => CatalogLoader.Parse(Minimal("kind: api", environments: environments)));

        Assert.Contains(expected, error.Message);
    }

    [Theory]
    [InlineData("environments: { dev: { hostSuffix: -X } }", "applications[0] (app).environments.dev: hostSuffix '-X' may only hold")]
    [InlineData("environments: { dev: { onDemand: maybe } }", "applications[0] (app).environments.dev: onDemand must be true or false")]
    public void GivenAnInvalidOverrideOfHostSuffixOrOnDemand_WhenParsed_ThenTheErrorNamesIt(string field, string expected)
    {
        var error = Assert.Throws<CatalogException>(() => CatalogLoader.Parse(Minimal("kind: api\n        " + field)));

        Assert.Contains(expected, error.Message);
    }

    [Fact]
    public void GivenAHostThatTheSuffixMakesLongerThanADnsLabel_WhenParsed_ThenEveryEnvironmentWhereItIsIsNamed()
    {
        var host = new string('h', 59);
        var yaml = Minimal($"kind: api\n        host: {host}",
            environments: "- { id: dev, name: Dev, hostSuffix: -dev }\n  - { id: hml, name: Hml, hostSuffix: -hml1 }\n  - { id: prod, name: Prod }");

        var error = Assert.Throws<CatalogException>(() => CatalogLoader.Parse(yaml));

        Assert.Contains($"applications[0] (app): host '{host}' with the hostSuffix '-hml1' of hml must be one DNS label of at most 63 characters",
            error.Message);
        // 59 + 4 is exactly 63: allowed.
        Assert.DoesNotContain("of dev", error.Message);
        Assert.DoesNotContain("of prod", error.Message);
    }

    [Fact]
    public void GivenADottedHost_WhenASuffixIsAppended_ThenItIsRefusedButWithoutOneItIsFine()
    {
        var yaml = Minimal("kind: api\n        host: api.heimdall",
            environments: "- { id: dev, name: Dev, hostSuffix: -dev }\n  - { id: prod, name: Prod }");

        var error = Assert.Throws<CatalogException>(() => CatalogLoader.Parse(yaml));

        Assert.Contains("host 'api.heimdall' with the hostSuffix '-dev' of dev must be one DNS label", error.Message);
        Assert.DoesNotContain("of prod", error.Message);
        Assert.Equal("api.heimdall", CatalogLoader.Parse(Minimal("kind: api\n        host: api.heimdall")).Applications.Single().Host);
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
    public void GivenTheHostsEnvironments_WhenNarrowingTheCatalogToThem_ThenOnlyTheirApplicationsAndTheirSystemsRemain()
    {
        var production = TestData.OnHost("production");
        var shared = TestData.OnHost();

        Assert.Equal([new EnvironmentDefinition("production", "Production")], production.Environments);
        Assert.Equal("artur-rios", production.Owner);
        // heimdall-worker is not deployed to production, and sandbox-api, the only application of
        // sandbox, is not either: the system goes with it.
        Assert.Equal(["heimdall", "yggdrasil"], production.Systems.Select(s => s.Id));
        Assert.Equal(["heimdall-api", "heimdall-ui", "traefik", "jenkins"], production.Applications.Select(a => a.Id));

        // scratch is local only, and local is not on this host.
        Assert.Equal(["heimdall", "yggdrasil", "sandbox"], shared.Systems.Select(s => s.Id));
        Assert.Contains(shared.Applications, a => a.Id == "heimdall-worker");
        Assert.Equal(["heimdall-api", "heimdall-ui"],
            HostCatalog.In(shared.Systems[0], shared.Environments.Single(e => e.Id == "production")).Select(a => a.Id));
    }

    [Fact]
    public void GivenTheHostsEnvironmentsInAnyOrder_WhenNarrowing_ThenTheyAreInCatalogOrder()
    {
        var host = TestData.OnHost("production", "development", "homologation");

        Assert.Equal(["development", "homologation", "production"], host.Environments.Select(e => e.Id));
    }

    [Fact]
    public void GivenEnvironmentIdsNotInTheCatalog_WhenNarrowing_ThenStartupIsRefusedNamingEachOne()
    {
        var error = Assert.Throws<StartupException>(() => HostCatalog.For(TestData.Catalog(), ["staging", "production", "qa"]));

        Assert.Contains("YGGDRASIL_ENVIRONMENTS: 'staging' is not an environment in the catalog (local, development, homologation, production)", error.Message);
        Assert.Contains("YGGDRASIL_ENVIRONMENTS: 'qa' is not an environment in the catalog", error.Message);
        Assert.DoesNotContain("'production'", error.Message);
    }

    [Fact]
    public void GivenTheLegacySetting_WhenAnIdIsNotInTheCatalog_ThenTheErrorNamesThatSetting()
    {
        var error = Assert.Throws<StartupException>(() => HostCatalog.For(TestData.Catalog(), ["staging"], "YGGDRASIL_ENVIRONMENT"));

        Assert.Contains("YGGDRASIL_ENVIRONMENT: 'staging' is not an environment", error.Message);
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
