using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using System.Text.Json.Nodes;
using JuiAI.ControlCenter.ViewModels;

namespace JuiAI.ControlCenter;

public sealed partial class MainWindow
{
    private static string DisplayVersion(JsonNode? version) =>
        version?.ToString() == "unknown" ? Locale.Status("unknown") : version?.ToString() ?? "";

    private async Task AgentsPage()
    {
        var services = (await vm.Client.Get("api/v1/services")).AsArray();
        var body = new StackPanel { Spacing = 16 };
        foreach (var item in services)
        {
            var value = item!; var id = value["id"]!.ToString();
            body.Children.Add(ItemRow(Stack(Text(id, 19), Text($"{DisplayVersion(value["version"])} · {(value["ready"]!.GetValue<bool>() ? Locale.Text("Reachable") : Locale.Text("Disconnected"))} · {(value["owned"]!.GetValue<bool>() ? Locale.Text("Managed by this app") : Locale.Text("Unmanaged"))}"), Text(value["path"]!.ToString())), Row(Command(Locale.Text("Start"), Symbol.Play, async () => { await vm.Client.Post("api/v1/services/" + id, new { action = "start" }); await ShowPage("agents"); }), Icon(Symbol.Stop, Locale.Text("Stop managed service"), async () => { if (await Confirm(Locale.Text("Stop service"), Locale.Text("Stop the app-managed service ") + id)) { await vm.Client.Post("api/v1/services/" + id, new { action = "stop" }); await ShowPage("agents"); } }), Icon(Symbol.Refresh, Locale.Text("Restart managed service"), async () => { if (await Confirm(Locale.Text("Restart service"), id)) { await vm.Client.Post("api/v1/services/" + id, new { action = "restart" }); await ShowPage("agents"); } }), Icon(Symbol.Document, Locale.Text("View logs"), async () => { var log = await vm.Client.Get("api/v1/logs/" + id); await Form(id + Locale.Text(" log"), Stack(Text(log["text"]!.ToString())), Locale.Text("Done")); }))));
        }
        Scrolled(body);
    }

    private void VoicePage()
    {
        var pivot = new Pivot();
        var search = vm.State["preferences"]!.AsArray().First(p => p!["id"]!.ToString() == "search")!;
        var connection = Combo(Locale.Text("Search connection"), vm.State["providers"]!.AsArray().Where(p => p!["protocol"]!.ToString() is "brave" or "tavily" or "searxng").Select(p => new Choice(p!["id"]!.ToString(), p["name"]!.ToString())), search["provider_id"]?.ToString());
        var enabled = new ToggleSwitch { OnContent = Locale.Text("On"), OffContent = Locale.Text("Off"), Header = Locale.Text("Search the web"), IsOn = search["enabled"]?.GetValue<bool>() ?? false };
        var language = Field(Locale.Text("Language"), search["language"]?.ToString() ?? "en"); var region = Field(Locale.Text("Region"), search["region"]?.ToString() ?? "us"); var count = Number(Locale.Text("Result count"), search["count"]?.GetValue<double>() ?? 5, 1, 10);
        void AdaptSearch()
        {
            var protocol = vm.State["providers"]!.AsArray().FirstOrDefault(p => p!["id"]!.ToString() == Selected(connection))?["protocol"]?.ToString();
            language.IsEnabled = protocol is "brave" or "searxng"; region.IsEnabled = protocol == "brave";
        }
        connection.SelectionChanged += (_, _) => AdaptSearch(); AdaptSearch();
        var test = Field(Locale.Text("Test query"), "technology news"); var results = new TextBox { IsReadOnly = true, AcceptsReturn = true, TextWrapping = TextWrapping.Wrap, MinHeight = 160, MaxHeight = 400 };
        var searchPanel = Stack(enabled, connection, language, region, count, Command(Locale.Text("Save search settings"), Symbol.Save, async () =>
        {
            if (enabled.IsOn && !await Confirm(Locale.Text("Enable search"), Locale.Text("The selected search service receives your query when you turn on web search in chat."))) return;
            await vm.Client.Put("api/v1/preferences/search", new { values = new { enabled = enabled.IsOn, provider_id = Selected(connection), language = language.Text, region = region.Text, count = (int)count.Value } }); await vm.Refresh(); ShowInfo(Locale.Text("Search settings saved."));
        }), test, Command(Locale.Text("Test search"), Symbol.Find, async () => { results.Text = (await vm.Client.Post("api/v1/search/test", new { query = test.Text })).ToJsonString(new() { WriteIndented = true }); }), results);
        pivot.Items.Add(new PivotItem { Header = Locale.Text("Web search"), Content = new ScrollViewer { Content = searchPanel } });
        var voice = vm.State["preferences"]!.AsArray().First(p => p!["id"]!.ToString() == "voice")!;
        var inputs = Combo(Locale.Text("Microphone"), audio.Inputs.Select((name, i) => new Choice(i.ToString(), name)), inputDevice.ToString());
        var outputs = Combo(Locale.Text("Playback device"), new[] { new Choice("-1", Locale.Text("System default")) }.Concat(audio.Outputs.Select((name, i) => new Choice(i.ToString(), name))), outputDevice.ToString());
        inputs.SelectionChanged += (_, _) => { if (int.TryParse(Selected(inputs), out var id)) inputDevice = id; };
        outputs.SelectionChanged += (_, _) => { if (int.TryParse(Selected(outputs), out var id)) outputDevice = id; };
        var autoSpeak = new ToggleSwitch { OnContent = Locale.Text("On"), OffContent = Locale.Text("Off"), Header = Locale.Text("Read replies aloud automatically"), IsOn = voice["auto_speak"]?.GetValue<bool>() ?? false };
        var sample = Field(Locale.Text("Preview text"), Locale.Text("Hello, I'm Jui. What would you like to work on today?"), true);
        var voicePanel = Stack(Text(Locale.Text("Speech recognition"), 18), Text(vm.State["models"]!.AsArray().FirstOrDefault(m => m!["id"]!.ToString() == vm.State["roles"]?["bindings"]?["asr"]?.ToString())?["name"]?.ToString() ?? Locale.Text("Unconfigured")), Text(Locale.Text("Speech output"), 18), Text(vm.State["models"]!.AsArray().FirstOrDefault(m => m!["id"]!.ToString() == vm.State["roles"]?["bindings"]?["tts"]?.ToString())?["name"]?.ToString() ?? Locale.Text("Unconfigured")), inputs, outputs, autoSpeak, Command(Locale.Text("Save voice preferences"), Symbol.Save, async () => { await vm.Client.Put("api/v1/preferences/voice", new { values = new { auto_speak = autoSpeak.IsOn } }); await vm.Refresh(); ShowInfo(Locale.Text("Saved.")); }), sample, Row(Command(Locale.Text("Preview"), Symbol.Volume, () => Speak(sample.Text)), Icon(Symbol.Stop, Locale.Text("Stop playback"), () => { StopSpeech(); return Task.CompletedTask; })));
        pivot.Items.Add(new PivotItem { Header = Locale.Text("Voice and devices"), Content = new ScrollViewer { Content = voicePanel } });
        pageHost.Children.Add(pivot);
    }

    private StackPanel ApprovalPanel(JsonNode value)
    {
        var status = value["status"]!.ToString(); var id = value["id"]!.ToString();
        var preview = value["preview"]?.ToString() ?? "";
        var panel = Stack(Text($"{value["agent"]} · {Locale.Status(value["action"]?.ToString())}", 16), Text(Locale.Format("Task {0} · {1}", value["run_id"]?.ToString()[..8], Locale.Status(status)), 12), Text(value["workspace"]?.ToString() ?? Locale.Text("Workspace not recorded"), 12), new TextBox { Text = preview, IsReadOnly = true, AcceptsReturn = true, TextWrapping = TextWrapping.Wrap, MaxHeight = 300, FontFamily = new Microsoft.UI.Xaml.Media.FontFamily("Cascadia Mono") });
        if (status == "pending")
            panel.Children.Add(Row(Command(Locale.Text("Approve once"), Symbol.Accept, async () =>
            {
                if (!await Confirm(Locale.Text("Approve action"), preview)) return;
                await vm.Client.Post($"api/v1/approvals/{id}/decision", new { accept = true }); if (page == "tools") await ShowPage("tools"); else await RefreshPendingApprovals();
            }), Command(Locale.Text("Reject"), Symbol.Cancel, async () => { await vm.Client.Post($"api/v1/approvals/{id}/decision", new { accept = false }); if (page == "tools") await ShowPage("tools"); else await RefreshPendingApprovals(); })));
        return panel;
    }
    private async Task ToolsPage()
    {
        var body = new StackPanel { Spacing = 16 }; var prefs = vm.State["preferences"]!.AsArray().First(p => p!["id"]!.ToString() == "tools")!;
        var switches = new Dictionary<string, ToggleSwitch>();
        foreach (var (id, label) in new[] { ("workspace", Locale.Text("Workspace files (writes require approval)")), ("git", Locale.Text("Git status (read-only)")), ("desktop", Locale.Text("Restricted desktop tools (actions require approval)")), ("commands", Locale.Text("Shell commands (approve each command, run as the current user)")) })
        {
            var toggle = new ToggleSwitch { OnContent = Locale.Text("On"), OffContent = Locale.Text("Off"), Header = label, IsOn = prefs[id]?.GetValue<bool>() ?? false }; switches[id] = toggle; body.Children.Add(toggle);
        }
        body.Children.Add(Command(Locale.Text("Save tool settings"), Symbol.Save, async () => { await vm.Client.Put("api/v1/preferences/tools", new { values = switches.ToDictionary(x => x.Key, x => x.Value.IsOn) }); await vm.Refresh(); ShowInfo(Locale.Text("Tool settings saved.")); }));
        body.Children.Add(Text(Locale.Text("Approval history"), 20));
        foreach (var value in (await vm.Client.Get("api/v1/approvals")).AsArray().Reverse()) body.Children.Add(ApprovalPanel(value!));
        Scrolled(body);
    }

    private async Task UpdatesPage(bool refresh = false)
    {
        if (page != "updates") return;
        pageHost.Children.Clear();
        var body = new StackPanel { Spacing = 16 };
        body.Children.Add(Row(Command(Locale.Text("Check for updates"), Symbol.Refresh, async () => { await vm.Client.Post("api/v1/updates/check"); await UpdatesPage(true); }), Command(Locale.Text("Install stable updates"), Symbol.Download, async () =>
        {
            if (await Confirm(Locale.Text("Install updates"), Locale.Text("Idle agent services managed by this app will stop before official stable updates are installed. Running external instances and uncommitted Hermes changes defer updates. Models and NVIDIA/CUDA components are excluded."))) { await vm.Client.Post("api/v1/updates/apply", new { stop_owned = true }); await UpdatesPage(true); }
        }), Icon(Symbol.Document, Locale.Text("Update log"), async () => { var value = await vm.Client.Get("api/v1/logs/updates"); await Form(Locale.Text("Update log"), Stack(Text(value["text"]!.ToString())), Locale.Text("Done")); })));
        var jobs = (await vm.Client.Get("api/v1/updates")).AsArray();
        if (jobs.LastOrDefault() is { } latest)
        {
            body.Children.Add(Text($"{(latest["apply"]?.GetValue<bool>() == true ? Locale.Text("Install updates") : Locale.Text("Check for updates"))} · {Locale.Status(latest["status"]?.ToString())}", 18));
            if (latest["status"]?.ToString() is "queued" or "running") body.Children.Add(new ProgressBar { IsIndeterminate = true });
            var result = latest["result"];
            if (result != null)
            {
                foreach (var id in new[] { "openclaw", "hermes", "opencode" })
                    body.Children.Add(ItemRow(Stack(Text(id, 18), Text(Locale.Text("Installed: ") + DisplayVersion(result["installed_after"]?[id] ?? result["installed"]?[id] ?? result["installed"]?["hermes_commit"])), Text(Locale.Text("Latest stable: ") + result["latest_releases"]?[id])), Text(result["updated"]?.AsArray().Any(x => x!.ToString() == id) == true ? Locale.Text("Updated") : Locale.Text("Checked"))));
                if (result["deferred"]?.AsArray().Count > 0) body.Children.Add(Text(Locale.Text("Deferred:\n") + result["deferred"]!.ToJsonString()));
                if (result["failed"]?.AsArray().Count > 0) body.Children.Add(Text(Locale.Text("Check or update failed:\n") + result["failed"]!.ToJsonString()));
            }
        }
        else body.Children.Add(Text(Locale.Text("Not checked yet")));
        Scrolled(body);
    }
    private void SettingsPage()
    {
        var language = Combo(Locale.Text("Display language"), new[] { new Choice("en-US", "English"), new Choice("zh-CN", "简体中文") }, Locale.Language);
        Microsoft.UI.Xaml.Automation.AutomationProperties.SetAutomationId(language, "DisplayLanguage");
        language.SelectionChanged += (_, _) =>
        {
            if (language.SelectedItem is Choice choice && choice.Id != Locale.Language) Run(() => SetLanguage(choice.Id));
        };
        Scrolled(Stack(language, Text("JuiAI Terminal", 24), Text("WinUI 3 · Windows App SDK 2.6.5 · .NET 10"), Text(vm.Client.Root), Text(Locale.Text("Current user · Local API · Encrypted API keys")), Command(Locale.Text("Export diagnostics"), Symbol.Download, async () => await Export("JuiAI-diagnostics", (await vm.Client.Get("api/v1/diagnostics")).ToJsonString(new() { WriteIndented = true }))), Command(Locale.Text("View backend log"), Symbol.Document, async () => { var value = await vm.Client.Get("api/v1/logs/workbench"); await Form(Locale.Text("Backend log"), Stack(Text(value["text"]!.ToString())), Locale.Text("Done")); }), Command(Locale.Text("Resume task execution"), Symbol.Refresh, async () =>
        {
            if (await Confirm(Locale.Text("Resume task execution"), Locale.Text("Stop agent services managed by this app and clear the uncertain task state after confirming that the previous action has stopped. Existing tasks will not rerun and external services will continue running.")))
            {
                foreach (var id in new[] { "openclaw", "hermes", "opencode" }) await vm.Client.Post("api/v1/services/" + id, new { action = "stop" }); await vm.Refresh(); ShowInfo(Locale.Text("Managed agents stopped. You can submit a new task."));
            }
        })));
    }
}
