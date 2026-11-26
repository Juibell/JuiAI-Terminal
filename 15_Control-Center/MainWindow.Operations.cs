using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using System.Text.Json.Nodes;
using JuiAI.ControlCenter.ViewModels;

namespace JuiAI.ControlCenter;

public sealed partial class MainWindow
{
    private async Task AgentsPage()
    {
        var services = (await vm.Client.Get("api/v1/services")).AsArray();
        var body = new StackPanel { Spacing = 16 };
        foreach (var item in services)
        {
            var value = item!; var id = value["id"]!.ToString();
            body.Children.Add(ItemRow(Stack(Text(id, 19), Text($"{value["version"]} · {(value["ready"]!.GetValue<bool>() ? "Reachable" : "Disconnected")} · {(value["owned"]!.GetValue<bool>() ? "Managed by this app" : "Unmanaged")}"), Text(value["path"]!.ToString())), Row(Command("Start", Symbol.Play, async () => { await vm.Client.Post("api/v1/services/" + id, new { action = "start" }); await ShowPage("agents"); }), Icon(Symbol.Stop, "Stop managed service", async () => { if (await Confirm("Stop service", "Stop the app-managed service " + id)) { await vm.Client.Post("api/v1/services/" + id, new { action = "stop" }); await ShowPage("agents"); } }), Icon(Symbol.Refresh, "Restart managed service", async () => { if (await Confirm("Restart service", id)) { await vm.Client.Post("api/v1/services/" + id, new { action = "restart" }); await ShowPage("agents"); } }), Icon(Symbol.Document, "View logs", async () => { var log = await vm.Client.Get("api/v1/logs/" + id); await Form(id + " log", Stack(Text(log["text"]!.ToString())), "Done"); }))));
        }
        Scrolled(body);
    }

    private void VoicePage()
    {
        var pivot = new Pivot();
        var search = vm.State["preferences"]!.AsArray().First(p => p!["id"]!.ToString() == "search")!;
        var connection = Combo("Search connection", vm.State["providers"]!.AsArray().Where(p => p!["protocol"]!.ToString() is "brave" or "tavily" or "searxng").Select(p => new Choice(p!["id"]!.ToString(), p["name"]!.ToString())), search["provider_id"]?.ToString());
        var enabled = new ToggleSwitch { Header = "Search the web", IsOn = search["enabled"]?.GetValue<bool>() ?? false };
        var language = Field("Language", search["language"]?.ToString() ?? "en"); var region = Field("Region", search["region"]?.ToString() ?? "us"); var count = Number("Result count", search["count"]?.GetValue<double>() ?? 5, 1, 10);
        void AdaptSearch()
        {
            var protocol = vm.State["providers"]!.AsArray().FirstOrDefault(p => p!["id"]!.ToString() == Selected(connection))?["protocol"]?.ToString();
            language.IsEnabled = protocol is "brave" or "searxng"; region.IsEnabled = protocol == "brave";
        }
        connection.SelectionChanged += (_, _) => AdaptSearch(); AdaptSearch();
        var test = Field("Test query", "technology news"); var results = new TextBox { IsReadOnly = true, AcceptsReturn = true, TextWrapping = TextWrapping.Wrap, MinHeight = 160, MaxHeight = 400 };
        var searchPanel = Stack(enabled, connection, language, region, count, Command("Save search settings", Symbol.Save, async () =>
        {
            if (enabled.IsOn && !await Confirm("Enable search", "The selected search service receives your query when you turn on web search in chat.")) return;
            await vm.Client.Put("api/v1/preferences/search", new { values = new { enabled = enabled.IsOn, provider_id = Selected(connection), language = language.Text, region = region.Text, count = (int)count.Value } }); await vm.Refresh(); ShowInfo("Search settings saved.");
        }), test, Command("Test search", Symbol.Find, async () => { results.Text = (await vm.Client.Post("api/v1/search/test", new { query = test.Text })).ToJsonString(new() { WriteIndented = true }); }), results);
        pivot.Items.Add(new PivotItem { Header = "Web search", Content = new ScrollViewer { Content = searchPanel } });
        var voice = vm.State["preferences"]!.AsArray().First(p => p!["id"]!.ToString() == "voice")!;
        var inputs = Combo("Microphone", audio.Inputs.Select((name, i) => new Choice(i.ToString(), name)), inputDevice.ToString());
        var outputs = Combo("Playback device", new[] { new Choice("-1", "System default") }.Concat(audio.Outputs.Select((name, i) => new Choice(i.ToString(), name))), outputDevice.ToString());
        inputs.SelectionChanged += (_, _) => { if (int.TryParse(Selected(inputs), out var id)) inputDevice = id; };
        outputs.SelectionChanged += (_, _) => { if (int.TryParse(Selected(outputs), out var id)) outputDevice = id; };
        var autoSpeak = new ToggleSwitch { Header = "Read replies aloud automatically", IsOn = voice["auto_speak"]?.GetValue<bool>() ?? false };
        var sample = Field("Preview text", "Hello, I'm Jui. What would you like to work on today?", true);
        var voicePanel = Stack(Text("Speech recognition", 18), Text(vm.State["models"]!.AsArray().FirstOrDefault(m => m!["id"]!.ToString() == vm.State["roles"]?["bindings"]?["asr"]?.ToString())?["name"]?.ToString() ?? "Unconfigured"), Text("Speech output", 18), Text(vm.State["models"]!.AsArray().FirstOrDefault(m => m!["id"]!.ToString() == vm.State["roles"]?["bindings"]?["tts"]?.ToString())?["name"]?.ToString() ?? "Unconfigured"), inputs, outputs, autoSpeak, Command("Save voice preferences", Symbol.Save, async () => { await vm.Client.Put("api/v1/preferences/voice", new { values = new { auto_speak = autoSpeak.IsOn } }); await vm.Refresh(); ShowInfo("Saved."); }), sample, Row(Command("Preview", Symbol.Volume, () => Speak(sample.Text)), Icon(Symbol.Stop, "Stop playback", () => { StopSpeech(); return Task.CompletedTask; })));
        pivot.Items.Add(new PivotItem { Header = "Voice and devices", Content = new ScrollViewer { Content = voicePanel } });
        pageHost.Children.Add(pivot);
    }

    private StackPanel ApprovalPanel(JsonNode value)
    {
        var status = value["status"]!.ToString(); var id = value["id"]!.ToString();
        var preview = value["preview"]?.ToString() ?? "";
        var panel = Stack(Text($"{value["agent"]} · {value["action"]}", 16), Text($"Task {value["run_id"]?.ToString()[..8]} · {status}", 12), Text(value["workspace"]?.ToString() ?? "Workspace not recorded", 12), new TextBox { Text = preview, IsReadOnly = true, AcceptsReturn = true, TextWrapping = TextWrapping.Wrap, MaxHeight = 300, FontFamily = new Microsoft.UI.Xaml.Media.FontFamily("Cascadia Mono") });
        if (status == "pending")
            panel.Children.Add(Row(Command("Approve once", Symbol.Accept, async () =>
            {
                if (!await Confirm("Approve action", preview)) return;
                await vm.Client.Post($"api/v1/approvals/{id}/decision", new { accept = true }); if (page == "tools") await ShowPage("tools"); else await RefreshPendingApprovals();
            }), Command("Reject", Symbol.Cancel, async () => { await vm.Client.Post($"api/v1/approvals/{id}/decision", new { accept = false }); if (page == "tools") await ShowPage("tools"); else await RefreshPendingApprovals(); })));
        return panel;
    }
    private async Task ToolsPage()
    {
        var body = new StackPanel { Spacing = 16 }; var prefs = vm.State["preferences"]!.AsArray().First(p => p!["id"]!.ToString() == "tools")!;
        var switches = new Dictionary<string, ToggleSwitch>();
        foreach (var (id, label) in new[] { ("workspace", "Workspace files (writes require approval)"), ("git", "Git status (read-only)"), ("desktop", "Restricted desktop tools (actions require approval)"), ("commands", "Shell commands (approve each command, run as the current user)") })
        {
            var toggle = new ToggleSwitch { Header = label, IsOn = prefs[id]?.GetValue<bool>() ?? false }; switches[id] = toggle; body.Children.Add(toggle);
        }
        body.Children.Add(Command("Save tool settings", Symbol.Save, async () => { await vm.Client.Put("api/v1/preferences/tools", new { values = switches.ToDictionary(x => x.Key, x => x.Value.IsOn) }); await vm.Refresh(); ShowInfo("Tool settings saved."); }));
        body.Children.Add(Text("Approval history", 20));
        foreach (var value in (await vm.Client.Get("api/v1/approvals")).AsArray().Reverse()) body.Children.Add(ApprovalPanel(value!));
        Scrolled(body);
    }

    private async Task UpdatesPage(bool refresh = false)
    {
        if (page != "updates") return;
        pageHost.Children.Clear();
        var body = new StackPanel { Spacing = 16 };
        body.Children.Add(Row(Command("Check for updates", Symbol.Refresh, async () => { await vm.Client.Post("api/v1/updates/check"); await UpdatesPage(true); }), Command("Install stable updates", Symbol.Download, async () =>
        {
            if (await Confirm("Install updates", "Idle agent services managed by this app will stop before official stable updates are installed. Running external instances and uncommitted Hermes changes defer updates. Models and NVIDIA/CUDA components are excluded.")) { await vm.Client.Post("api/v1/updates/apply", new { stop_owned = true }); await UpdatesPage(true); }
        }), Icon(Symbol.Document, "Update log", async () => { var value = await vm.Client.Get("api/v1/logs/updates"); await Form("Update log", Stack(Text(value["text"]!.ToString())), "Done"); })));
        var jobs = (await vm.Client.Get("api/v1/updates")).AsArray();
        if (jobs.LastOrDefault() is { } latest)
        {
            body.Children.Add(Text($"{(latest["apply"]?.GetValue<bool>() == true ? "Install updates" : "Check for updates")} · {latest["status"]}", 18));
            if (latest["status"]?.ToString() is "queued" or "running") body.Children.Add(new ProgressBar { IsIndeterminate = true });
            var result = latest["result"];
            if (result != null)
            {
                foreach (var id in new[] { "openclaw", "hermes", "opencode" })
                    body.Children.Add(ItemRow(Stack(Text(id, 18), Text("Installed: " + (result["installed_after"]?[id] ?? result["installed"]?[id] ?? result["installed"]?["hermes_commit"])), Text("Latest stable: " + result["latest_releases"]?[id])), Text(result["updated"]?.AsArray().Any(x => x!.ToString() == id) == true ? "Updated" : "Checked")));
                if (result["deferred"]?.AsArray().Count > 0) body.Children.Add(Text("Deferred:\n" + result["deferred"]!.ToJsonString()));
                if (result["failed"]?.AsArray().Count > 0) body.Children.Add(Text("Check or update failed:\n" + result["failed"]!.ToJsonString()));
            }
        }
        else body.Children.Add(Text("Not checked yet"));
        Scrolled(body);
    }
    private void SettingsPage()
    {
        Scrolled(Stack(Text("JuiAI Terminal", 24), Text("WinUI 3 · Windows App SDK 2.6.5 · .NET 10"), Text(vm.Client.Root), Text("Current user · Local API · Encrypted API keys"), Command("Export diagnostics", Symbol.Download, async () => await Export("JuiAI-diagnostics", (await vm.Client.Get("api/v1/diagnostics")).ToJsonString(new() { WriteIndented = true }))), Command("View backend log", Symbol.Document, async () => { var value = await vm.Client.Get("api/v1/logs/workbench"); await Form("Backend log", Stack(Text(value["text"]!.ToString())), "Done"); }), Command("Resume task execution", Symbol.Refresh, async () =>
        {
            if (await Confirm("Resume task execution", "Stop agent services managed by this app and clear the uncertain task state after confirming that the previous action has stopped. Existing tasks will not rerun and external services will continue running."))
            {
                foreach (var id in new[] { "openclaw", "hermes", "opencode" }) await vm.Client.Post("api/v1/services/" + id, new { action = "stop" }); await vm.Refresh(); ShowInfo("Managed agents stopped. You can submit a new task.");
            }
        })));
    }
}
