using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;
using System.Diagnostics;

namespace JuiAI.ControlCenter.Services;

public sealed class JuiClient : IDisposable
{
    private readonly HttpClient http;
    public string Root { get; } = FindRoot();
    public bool OwnsBackend { get; private set; }
    public JuiClient(HttpMessageHandler? handler = null)
    {
        http = handler == null ? new HttpClient() : new HttpClient(handler);
        http.Timeout = Timeout.InfiniteTimeSpan;
        http.BaseAddress = new Uri("http://127.0.0.1:12394/");
    }
    public static string FindRoot()
    {
        var directory = new DirectoryInfo(AppContext.BaseDirectory);
        while (directory != null)
        {
            if (File.Exists(Path.Combine(directory.FullName, "05_Agent-Router", "settings.json"))) return directory.FullName;
            directory = directory.Parent;
        }
        throw new InvalidOperationException("Keep the app inside the extracted JuiAI Terminal project folder.");
    }

    public async Task Connect()
    {
        var settings = JsonNode.Parse(await File.ReadAllTextAsync(Path.Combine(Root, "05_Agent-Router", "settings.json")))!;
        http.BaseAddress = new Uri($"http://127.0.0.1:{settings["port"]}/");
        JsonNode? health = null;
        try { health = await Get("api/health", 3); } catch (HttpRequestException) { } catch (TaskCanceledException) { }
        if (health == null)
        {
            await Script("Start-Jui.ps1", "-Native");
            health = await Get("api/health", 10);
            OwnsBackend = true;
        }
        if (health?["control_api"]?.GetValue<int>() != 1 || health["native"]?.GetValue<bool>() != true)
            throw new InvalidOperationException("The legacy web workbench is running. Close it normally, then open JuiAI Terminal.");
        var encrypted = await File.ReadAllBytesAsync(Path.Combine(Root, "05_Agent-Router", ".runtime", "vault", "desktop-auth.bin"));
        var token = Encoding.UTF8.GetString(ProtectedData.Unprotect(encrypted, null, DataProtectionScope.CurrentUser));
        http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", token);
        http.DefaultRequestHeaders.Add("X-Jui-Request", "1");
    }

    public async Task Script(string file, string arguments)
    {
        var start = new ProcessStartInfo("powershell.exe")
        {
            UseShellExecute = false, CreateNoWindow = true,
            Arguments = $"-NoProfile -ExecutionPolicy Bypass -File \"{Path.Combine(Root, file)}\" {arguments}"
        };
        using var process = Process.Start(start) ?? throw new InvalidOperationException("Could not start the local service.");
        await process.WaitForExitAsync();
        if (process.ExitCode != 0) throw new InvalidOperationException("Service startup failed. Check the local JuiAI Terminal logs.");
    }

    public Task<JsonNode> Get(string path, int seconds = 120) => Send(HttpMethod.Get, path, null, seconds);
    public Task<JsonNode> Post(string path, object? body = null, int seconds = 180) => Send(HttpMethod.Post, path, body ?? new { }, seconds);
    public Task<JsonNode> Put(string path, object body) => Send(HttpMethod.Put, path, body);
    public Task<JsonNode> Patch(string path, object body) => Send(HttpMethod.Patch, path, body);
    public Task<JsonNode> Delete(string path) => Send(HttpMethod.Delete, path);

    private async Task<JsonNode> Send(HttpMethod method, string path, object? body = null, int seconds = 180, CancellationToken cancellation = default)
    {
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellation);
        timeout.CancelAfter(TimeSpan.FromSeconds(seconds));
        using var request = new HttpRequestMessage(method, path);
        if (body != null) request.Content = new StringContent(System.Text.Json.JsonSerializer.Serialize(body), Encoding.UTF8, "application/json");
        timeout.Token.ThrowIfCancellationRequested();
        using var response = await http.SendAsync(request, timeout.Token);
        var text = await response.Content.ReadAsStringAsync(timeout.Token);
        JsonNode? value = null;
        try { value = JsonNode.Parse(text); } catch (System.Text.Json.JsonException) { }
        if (!response.IsSuccessStatusCode)
            throw new InvalidOperationException(value?["error"]?["message"]?.ToString() ?? value?["detail"]?.ToString() ?? $"Request failed: {(int)response.StatusCode}");
        return value ?? new JsonObject();
    }

    public async Task<JsonNode> Transcribe(byte[] wav, string workspace, CancellationToken token)
        => await TranscribeAt("api/v1/voice/transcribe?workspace_id=" + Uri.EscapeDataString(workspace), wav, token);

    public Task<JsonNode> DebugText(object body, CancellationToken token) => Send(HttpMethod.Post, "api/v1/debug/text", body, 600, token);
    public Task<JsonNode> DebugSearch(object body, CancellationToken token) => Send(HttpMethod.Post, "api/v1/debug/search", body, 45, token);
    public Task<byte[]> DebugSpeech(object body, CancellationToken token) => SpeechAt("api/v1/debug/speech", body, token);
    public Task<JsonNode> DebugTranscribe(byte[] wav, string model, string workspace, string identity, bool confirmExternal, CancellationToken token)
        => TranscribeAt($"api/v1/debug/transcribe?model_ref={Uri.EscapeDataString(model)}&workspace_id={Uri.EscapeDataString(workspace)}&request_id={identity}&confirm_external={confirmExternal.ToString().ToLowerInvariant()}", wav, token);

    private async Task<JsonNode> TranscribeAt(string path, byte[] wav, CancellationToken token)
    {
        using var content = new ByteArrayContent(wav);
        content.Headers.ContentType = new MediaTypeHeaderValue("audio/wav");
        using var response = await http.PostAsync(path, content, token);
        var value = JsonNode.Parse(await response.Content.ReadAsStringAsync(token))!;
        if (!response.IsSuccessStatusCode) throw new InvalidOperationException(value["error"]?["message"]?.ToString() ?? "Speech recognition failed.");
        return value;
    }

    public Task<byte[]> Speak(object body, CancellationToken token) => SpeechAt("api/v1/voice/speak", body, token);

    private async Task<byte[]> SpeechAt(string path, object body, CancellationToken token)
    {
        using var content = new StringContent(System.Text.Json.JsonSerializer.Serialize(body), Encoding.UTF8, "application/json");
        using var response = await http.PostAsync(path, content, token);
        if (!response.IsSuccessStatusCode)
        {
            var value = JsonNode.Parse(await response.Content.ReadAsStringAsync(token));
            throw new InvalidOperationException(value?["error"]?["message"]?.ToString() ?? "Speech output failed.");
        }
        return await response.Content.ReadAsByteArrayAsync(token);
    }

    public async Task Events(Func<JsonNode, Task> receive, CancellationToken token, long after = 0)
    {
        long cursor = after;
        while (!token.IsCancellationRequested)
        {
            try
            {
                using var response = await http.GetAsync("api/v1/events?after=" + cursor, HttpCompletionOption.ResponseHeadersRead, token);
                response.EnsureSuccessStatusCode();
                using var reader = new StreamReader(await response.Content.ReadAsStreamAsync(token));
                while (await reader.ReadLineAsync(token) is { } line)
                {
                    if (!line.StartsWith("data: ", StringComparison.Ordinal)) continue;
                    var value = JsonNode.Parse(line[6..])!;
                    cursor = value["seq"]!.GetValue<long>();
                    await receive(value);
                }
            }
            catch (OperationCanceledException) when (token.IsCancellationRequested) { return; }
            catch (HttpRequestException) { }
            catch (IOException) { }
            await Task.Delay(1500, token);
        }
    }

    public void Dispose() => http.Dispose();
}
