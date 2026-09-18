using System.Diagnostics;
using System.Net.Sockets;
using Yggdrasil.Status.Model;

namespace Yggdrasil.Status.Probing;

/// <summary>
/// One GET to an application's health URL. Never throws: every way a probe can fail is a result,
/// with a short error the console can show as is.
/// </summary>
public sealed class HealthProber(HttpClient http, TimeProvider time)
{
    public static readonly TimeSpan Timeout = TimeSpan.FromSeconds(5);

    public async Task<ProbeResult> ProbeAsync(Uri url, CancellationToken cancellationToken)
    {
        // The timeout is per probe rather than HttpClient.Timeout so a timeout can be told apart
        // from the service shutting down.
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        timeout.CancelAfter(Timeout);

        var started = Stopwatch.GetTimestamp();
        try
        {
            // Headers only: the body of a health endpoint says nothing the status code doesn't, and
            // some (Jenkins' /login) are whole pages.
            using var response = await http.GetAsync(url, HttpCompletionOption.ResponseHeadersRead, timeout.Token);
            var statusCode = (int)response.StatusCode;
            // A response with any status is an answer, so error stays null: statusCode says the rest.
            return Result(statusCode is >= 200 and < 300, statusCode, null);
        }
        catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
        {
            return Result(false, null, "timeout");
        }
        catch (HttpRequestException e)
        {
            return Result(false, null, Describe(e));
        }

        ProbeResult Result(bool healthy, int? statusCode, string? error) =>
            new(healthy, statusCode, (long)Stopwatch.GetElapsedTime(started).TotalMilliseconds, time.GetUtcNow(), error);
    }

    private static string Describe(HttpRequestException e)
    {
        if (e.InnerException is SocketException socket)
        {
            switch (socket.SocketErrorCode)
            {
                case SocketError.ConnectionRefused:
                    return "connection refused";
                case SocketError.HostNotFound or SocketError.TryAgain or SocketError.NoData:
                    return "name not resolved";
                case SocketError.HostUnreachable or SocketError.NetworkUnreachable:
                    return "host unreachable";
                case SocketError.ConnectionReset:
                    return "connection reset";
            }
        }

        return e.HttpRequestError switch
        {
            HttpRequestError.NameResolutionError => "name not resolved",
            HttpRequestError.ConnectionError => "connection failed",
            HttpRequestError.SecureConnectionError => "tls error",
            HttpRequestError.ResponseEnded => "connection closed",
            HttpRequestError.InvalidResponse => "invalid response",
            _ => "request failed",
        };
    }
}
