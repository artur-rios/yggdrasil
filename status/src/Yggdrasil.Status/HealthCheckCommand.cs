namespace Yggdrasil.Status;

/// <summary>
/// The container health check, run as a second process inside the container. It asks the running
/// service's own /healthz over loopback and turns the answer into an exit code for Docker.
/// </summary>
public static class HealthCheckCommand
{
    public static async Task<int> RunAsync()
    {
        using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(3) };
        try
        {
            using var response = await http.GetAsync($"http://127.0.0.1:{StatusOptions.PublicPort}/healthz");
            return response.IsSuccessStatusCode ? 0 : 1;
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException)
        {
            await Console.Error.WriteLineAsync($"healthcheck: {e.Message}");
            return 1;
        }
    }
}
