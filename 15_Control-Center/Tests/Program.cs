using System.Net;
using JuiAI.ControlCenter;
using System.Text;
using System.Text.Json.Nodes;
using JuiAI.ControlCenter.Services;
using JuiAI.ControlCenter.ViewModels;

int checks = 0;
var state = """
{"workspace_id":"test","models":[{"id":"main","name":"Main","enabled":true,"capabilities":["text","tools"]},{"id":"alternate","name":"Alternate","enabled":true,"capabilities":["text"]},{"id":"disabled","name":"Disabled","enabled":false,"capabilities":["text"]},{"id":"audio","name":"Voice","enabled":true,"capabilities":["tts"]}],"roles":{"id":"global","bindings":{"main":"main","code":"main"},"agents":{"opencode":"main"}},"workspaces":[{"id":"test","name":"Test workspace"}]}
""";
JsonObject? mainPatch = null;
HttpMethod? mainMethod = null;
var transport = new FakeHandler(request => request.RequestUri!.AbsolutePath switch
{
    "/api/v1/state" => (HttpStatusCode.OK, state),
    "/api/v1/roles/main" => ChangeMain(request),
    "/api/v1/conversations" => (HttpStatusCode.OK, "[{\"id\":\"a\",\"title\":\"Older\",\"favorite\":false,\"updated\":\"2026-01-01\"},{\"id\":\"b\",\"title\":\"Pinned\",\"favorite\":true,\"updated\":\"2025-01-01\"}]"),
    _ => (HttpStatusCode.TooManyRequests, "{\"error\":{\"code\":\"rate_limited\",\"message\":\"rate limited\"}}")
});
using var client = new JuiClient(transport);
var vm = new ControlViewModel(client);
await vm.Refresh();
Check(vm.WorkspaceId == "test", "workspace selection");
Check(vm.ModelChoices("text").Select(choice => choice.Id).SequenceEqual(["main", "alternate"]), "enabled/capability filtering");
Check(vm.ModelChoices("tts").Single().Id == "audio", "voice role filtering");
Check(vm.WorkspaceChoices().Single().Name == "Test workspace", "workspace contract");
Check(vm.MainModelId == "main" && vm.MainModelName == "Main", "current default main model");
await vm.SetMainModel("alternate");
Check(mainMethod == HttpMethod.Patch && mainPatch?.Count == 1 && mainPatch["model_ref"]?.ToString() == "alternate", "change only the default main model");
Check(vm.MainModelId == "alternate" && vm.MainModelName == "Alternate" && vm.State["roles"]?["bindings"]?["code"]?.ToString() == "main" && vm.State["roles"]?["agents"]?["opencode"]?.ToString() == "main", "apply updated role state from server");
try { await vm.SetMainModel("disabled"); throw new Exception("Disabled model switch was accepted"); }
catch (InvalidOperationException) { Check(vm.MainModelId == "alternate", "keep default visible when switch is rejected"); }
await vm.RefreshConversations();
Check(vm.Conversations[0].Id == "b", "favorites sort before recency");
bool changed = false;
vm.PropertyChanged += (_, args) => changed |= args.PropertyName == "Status";
vm.Status = "ready";
Check(changed, "MVVM status notifications");
try { await client.Get("error"); throw new Exception("Error response was not rejected"); }
catch (InvalidOperationException error) { Check(error.Message == "rate limited", "unified error contract"); }
string? debugBody = null; string? debugPath = null;
using var debugClient = new JuiClient(new FakeHandler(request =>
{
    debugPath = request.RequestUri!.PathAndQuery;
    debugBody = request.Content?.ReadAsStringAsync().GetAwaiter().GetResult();
    return (HttpStatusCode.OK, "{\"text\":\"Test reply\"}");
}));
await debugClient.DebugText(new { model_ref = "independent", prompt = "Hello", mode = "text" }, CancellationToken.None);
Check(debugPath == "/api/v1/debug/text" && debugBody!.Contains("independent"), "independent text model target");
await debugClient.DebugTranscribe(new byte[] { 1, 2, 3 }, "asr-model", "test", "recognition-test", false, CancellationToken.None);
Check(debugPath!.Contains("model_ref=asr-model") && debugPath.Contains("request_id=recognition-test"), "independent recognition model target");
using var cancelled = new CancellationTokenSource(); cancelled.Cancel();
try { await debugClient.DebugText(new { model_ref = "independent" }, cancelled.Token); throw new Exception("Cancelled debug request was sent"); }
catch (OperationCanceledException) { Check(true, "debug cancellation token"); }
var preferenceRoot = Directory.CreateTempSubdirectory("jui-locale-");
try
{
    System.Globalization.CultureInfo.CurrentUICulture = new("zh-CN");
    Locale.Load(preferenceRoot.FullName);
    Check(Locale.Language == "en-US" && Locale.Text("Copy code") == "Copy code", "fresh install defaults to English on any system");
    var headers = new List<string>();
    using var languageClient = new JuiClient(new FakeHandler(request =>
    {
        headers.Add(request.Headers.AcceptLanguage.Single().Value);
        return (HttpStatusCode.OK, "{}");
    }));
    await languageClient.Get("test");
    Locale.Save(preferenceRoot.FullName, Locale.Chinese);
    Locale.Load(preferenceRoot.FullName);
    Check(Locale.Text("Copy code") == "复制代码", "Chinese preference survives reloading");
    await languageClient.Get("test");
    Check(headers.SequenceEqual(["en-US", "zh-CN"]), "language change reaches new requests on the same connection");
    Check(vm.MainModelName == "Alternate" && vm.WorkspaceChoices().Single().Name == "Test workspace", "user-authored names stay unchanged");
    using var titleClient = new JuiClient(new FakeHandler(_ => (HttpStatusCode.OK, """
        [{"id":"generated","title":"New chat","title_is_placeholder":true},{"id":"custom","title":"New chat","title_is_placeholder":false},{"id":"legacy","title":"New chat"}]
        """)));
    var history = new ControlViewModel(titleClient);
    await history.RefreshConversations();
    Check(history.Conversations.Single(c => c.Id == "generated").Name == "新对话" &&
        history.Conversations.Where(c => c.Id != "generated").All(c => c.Name == "New chat"),
        "only explicitly generated chat titles are localized");
    var path = Path.Combine(preferenceRoot.FullName, "05_Agent-Router", ".runtime", "ui-language.txt");
    File.WriteAllText(path, "not-a-locale");
    Locale.Load(preferenceRoot.FullName);
    Check(Locale.Language == "en-US", "invalid preference defaults to English");
}
finally { preferenceRoot.Delete(true); }
Console.WriteLine($"{checks} client logic checks passed.");

(HttpStatusCode, string) ChangeMain(HttpRequestMessage request)
{
    mainMethod = request.Method;
    mainPatch = JsonNode.Parse(request.Content!.ReadAsStringAsync().GetAwaiter().GetResult())!.AsObject();
    return mainPatch["model_ref"]?.ToString() == "alternate"
        ? (HttpStatusCode.OK, """{"id":"global","bindings":{"main":"alternate","code":"main"},"agents":{"opencode":"main"}}""")
        : (HttpStatusCode.BadRequest, """{"error":{"message":"model unavailable"}}""");
}

void Check(bool value, string label)
{
    if (!value) throw new Exception("Failed: " + label);
    checks++;
    Console.WriteLine("PASS " + label);
}

sealed class FakeHandler(Func<HttpRequestMessage, (HttpStatusCode, string)> respond) : HttpMessageHandler
{
    protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken token)
    {
        var (code, body) = respond(request);
        return Task.FromResult(new HttpResponseMessage(code) { Content = new StringContent(body, Encoding.UTF8, "application/json") });
    }
}
