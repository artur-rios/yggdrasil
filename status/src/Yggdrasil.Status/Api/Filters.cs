using System.Security.Cryptography;
using System.Text;
using Microsoft.Net.Http.Headers;

namespace Yggdrasil.Status.Api;

/// <summary>
/// Every /api/* endpoint needs "Authorization: Bearer &lt;YGGDRASIL_STATUS_TOKEN&gt;". An endpoint
/// filter rather than middleware so that CORS preflights, which carry no Authorization header by
/// design, are answered by the CORS middleware before this ever runs.
/// </summary>
public sealed class BearerTokenFilter(StatusOptions options) : IEndpointFilter
{
    private const string Scheme = "Bearer ";

    // Hashing both sides first makes the comparison constant-time in the length too: FixedTimeEquals
    // returns early on a length mismatch, which would tell a caller how long the token is.
    private readonly byte[] expected = SHA256.HashData(Encoding.UTF8.GetBytes(options.Token));

    public async ValueTask<object?> InvokeAsync(EndpointFilterInvocationContext context, EndpointFilterDelegate next)
    {
        var header = context.HttpContext.Request.Headers.Authorization.ToString();

        if (!header.StartsWith(Scheme, StringComparison.OrdinalIgnoreCase) ||
            !CryptographicOperations.FixedTimeEquals(expected, SHA256.HashData(Encoding.UTF8.GetBytes(header[Scheme.Length..].Trim()))))
        {
            // Empty body, as the contract says; the header is what RFC 6750 asks of a 401.
            context.HttpContext.Response.Headers[HeaderNames.WWWAuthenticate] = "Bearer";
            return Results.Unauthorized();
        }

        return await next(context);
    }
}

/// <summary>
/// Decides whether a request arrived on the internal port. A service of its own because TestServer
/// has no sockets (every request reports local port 0), so tests replace it; in production it is the
/// socket's local port and nothing a client sends.
/// </summary>
public class InternalPortPolicy(StatusOptions options)
{
    public virtual bool IsInternal(HttpContext context) => IsInternalPort(context.Connection.LocalPort, options.InternalPort);

    // Never the Host header: Traefik forwards the client's own, so any caller could name port 8081
    // in it. The port the connection actually landed on is the one thing Traefik can't reach, since
    // it only ever forwards to 8080.
    public static bool IsInternalPort(int localPort, int internalPort) => localPort == internalPort;
}

/// <summary>/internal/* is 404 -- not 403 -- off the internal port: from outside it does not exist.</summary>
public sealed class InternalPortFilter(InternalPortPolicy policy) : IEndpointFilter
{
    public async ValueTask<object?> InvokeAsync(EndpointFilterInvocationContext context, EndpointFilterDelegate next) =>
        policy.IsInternal(context.HttpContext) ? await next(context) : Results.NotFound();
}
