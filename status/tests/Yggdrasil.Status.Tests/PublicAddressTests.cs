using Yggdrasil.Status.Docker;
using Yggdrasil.Status.Model;

namespace Yggdrasil.Status.Tests;

/// <summary>
/// The address each application is reported under (<c>url</c>): the one scripts/catalog.py routes
/// (<c>public-host</c> and <c>public-path</c>), which is what the console links to.
/// </summary>
public class PublicAddressTests
{
    private static readonly Observation Up = new(new DockerObservation.Found(
        new ContainerDetails("c", "running", null, TestData.Now.AddDays(-1), false, "i", new Dictionary<string, string>())), TestData.Probe());

    // Two environments, one with a suffix: enough to see the suffix go before the domain, and not after.
    private static string Catalog(string application, string kind = "web") => $$"""
        owner: artur-rios
        environments:
          - { id: development, name: Development, hostSuffix: -dev }
          - { id: production, name: Production }
        systems:
          - id: s
            name: S
            applications:
              - id: app
                name: App
                kind: {{kind}}
                health: http://app:8080/healthz
        {{application}}
        """;

    private static string? UrlOf(string application, string environment = "development", string kind = "web")
    {
        var host = HostCatalog.For(CatalogLoader.Parse(Catalog(application, kind)), ["development", "production"]);
        var app = host.Applications.Single();
        return new SnapshotBuilder(host, TestData.Options("example.com"))
            .BuildApplication(ProbeTarget.Of(app, environment), Up, TestData.Now).Url;
    }

    [Fact]
    public void GivenAnApplicationWithADomain_WhenReported_ThenItsUrlIsUnderThatDomain()
    {
        Assert.Equal("https://www-dev.acme.dev", UrlOf("        host: www\n        domain: acme.dev"));
        Assert.Equal("https://www.acme.dev", UrlOf("        host: www\n        domain: acme.dev", "production"));
    }

    [Fact]
    public void GivenADocsSiteWithAPath_WhenReported_ThenItsUrlEndsWithThePath()
    {
        Assert.Equal("https://docs-dev.example.com/docs", UrlOf("        host: docs\n        path: /docs"));
        Assert.Equal("https://help.acme.dev/guides/start",
            UrlOf("        host: help\n        domain: acme.dev\n        path: /guides/start", "production"));
    }

    [Fact]
    public void GivenAnApexSite_WhenReported_ThenItsUrlIsTheBareDomain()
    {
        // The bare domain has no suffix to take: it is the same in every environment.
        Assert.Equal("https://example.com", UrlOf("        apex: true"));
        Assert.Equal("https://example.com", UrlOf("        apex: true", "production"));
        Assert.Equal("https://acme.dev/blog", UrlOf("        apex: true\n        domain: acme.dev\n        path: /blog"));
    }

    [Fact]
    public void GivenAHostAndApex_WhenReported_ThenTheHostWins()
    {
        // As in catalog.py: the bare domain is served too, but the address is the host's.
        Assert.Equal("https://site-dev.example.com", UrlOf("        host: site\n        apex: true"));
    }

    [Fact]
    public void GivenNeitherAHostNorApex_WhenReported_ThenThereIsNoUrlEvenWithAPath()
    {
        Assert.Null(UrlOf("        path: /docs"));
        Assert.Null(UrlOf("        domain: acme.dev"));
    }

    [Fact]
    public void GivenAPlatformComponent_WhenReported_ThenItKeepsTheHostsDomainAndHasNoPath()
    {
        Assert.Equal("https://app.example.com",
            UrlOf("        host: app\n        domain: acme.dev\n        path: /x\n        apex: true", kind: "platform"));
    }

    [Theory]
    [InlineData("domain: acme", "domain 'acme'")]
    [InlineData("domain: Acme.dev", "domain 'Acme.dev'")]
    [InlineData("domain: 'https://acme.dev'", "domain 'https://acme.dev'")]
    [InlineData("path: docs", "path 'docs'")]
    [InlineData("path: /docs/", "path '/docs/'")]
    [InlineData("path: /a/../b", "path '/a/../b'")]
    [InlineData("apex: maybe", "apex must be true or false")]
    public void GivenAnInvalidDomainPathOrApex_WhenParsed_ThenTheErrorNamesIt(string field, string expected)
    {
        var error = Assert.Throws<CatalogException>(() => CatalogLoader.Parse(Catalog("        host: x\n        " + field)));

        Assert.Contains($"applications[0] (app): {expected}", error.Message);
    }

    [Fact]
    public void GivenKeysWithNoValue_WhenReported_ThenTheyAreUnsetAsInCatalogPy()
    {
        // YAML's `domain:` is null to PyYAML, which catalog.py accepts as no domain.
        Assert.Equal("https://x-dev.example.com", UrlOf("        host: x\n        domain:\n        path:\n        apex:"));
    }

    // scripts/test_status_parity.py runs catalog.py on the same catalog and checks it against the same list.
    [Fact]
    public void GivenTheParityCatalog_WhenReported_ThenEveryUrlIsTheOneCatalogPyForms()
    {
        var fixtures = Path.Combine(AppContext.BaseDirectory, "fixtures");
        var host = HostCatalog.For(CatalogLoader.LoadFile(Path.Combine(fixtures, "public-addresses.yaml")), ["development", "production"]);
        var builder = new SnapshotBuilder(host, TestData.Options("example.com"));

        var expected = File.ReadAllLines(Path.Combine(fixtures, "public-addresses.expected.txt"))
            .Where(line => line.Trim().Length > 0 && !line.StartsWith('#'))
            .Select(line => line.Split(' '))
            .ToList();

        Assert.NotEmpty(expected);
        Assert.Equal(host.Applications.Select(a => a.Id).Order(), expected.Select(row => row[0]).Distinct().Order());
        foreach (var row in expected)
        {
            var (id, environment, address) = (row[0], row[1], row[2]);
            var target = ProbeTarget.Of(host.Applications.Single(a => a.Id == id), environment);

            Assert.Equal(address == "-" ? null : address, builder.BuildApplication(target, Up, TestData.Now).Url);
        }
    }
}
