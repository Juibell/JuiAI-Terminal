using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using System.Text.Json.Nodes;
using JuiAI.ControlCenter.ViewModels;

namespace JuiAI.ControlCenter;

public sealed partial class MainWindow
{
    private static TextBox Field(string header, string value = "", bool multi = false) => new() { Header = header, Text = value, TextWrapping = TextWrapping.Wrap, AcceptsReturn = multi, MinHeight = multi ? 140 : 0, HorizontalAlignment = HorizontalAlignment.Stretch };
    private static NumberBox Number(string header, double value, double min, double max) => new() { Header = header, Value = value, Minimum = min, Maximum = max, SpinButtonPlacementMode = NumberBoxSpinButtonPlacementMode.Compact };
    private async Task<bool> Form(string title, StackPanel content, string primary = "Save") => await new ContentDialog { XamlRoot = Root.XamlRoot, RequestedTheme = ElementTheme.Dark, Title = title, PrimaryButtonText = primary, CloseButtonText = "Cancel", DefaultButton = ContentDialogButton.Close, Content = new ScrollViewer { Content = content, MaxHeight = 570, MinWidth = 390, HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled } }.ShowAsync() == ContentDialogResult.Primary;

    private void ModelsPage()
    {
        var pivot = new Pivot();
        var providers = new StackPanel { Spacing = 12 };
        providers.Children.Add(Row(Command("Add connection", Symbol.Add, () => ProviderDialog()), Command("Export configuration", Symbol.Download, async () => await Export("JuiAI-configuration", (await vm.Client.Get("api/v1/configuration/export")).ToJsonString(new() { WriteIndented = true })))));
        foreach (var item in vm.State["providers"]!.AsArray())
        {
            var value = item!; var identity = value["id"]!.ToString();
            var local = value["local"]?.GetValue<bool>() == true;
            var label = Stack(Text(value["name"]!.ToString(), 16), Text($"{value["protocol"]} · {(local ? "Local" : "External API")} · {(value["enabled"]?.GetValue<bool>() == true ? "Enabled" : "Disabled")}"), Text(value["base_url"]?.ToString() ?? ""));
            var commands = Row(Icon(Symbol.Setting, "Edit connection", () => ProviderDialog(value)), Icon(Symbol.Link, "List models / Test connection", async () =>
            {
                if (value["protocol"]!.ToString() is "brave" or "tavily" or "searxng") { ShowInfo("Save a search connection on the search page before testing."); return; }
                var result = await vm.Client.Post($"api/v1/providers/{identity}/probe", new { generate = false });
                await Form("Connection result", Stack(Text(result.ToJsonString(new() { WriteIndented = true }))), "Done");
            }), Icon(Symbol.Delete, "Delete connection", async () => { if (await Confirm("Delete connection", value["name"]!.ToString())) { await vm.Client.Delete($"api/v1/providers/{identity}"); await ReloadPage(); } }));
            providers.Children.Add(ItemRow(label, commands));
        }
        pivot.Items.Add(new PivotItem { Header = "Provider connections", Content = new ScrollViewer { Content = providers } });
        var models = new StackPanel { Spacing = 12 }; models.Children.Add(Command("Add model profile", Symbol.Add, () => ModelDialog()));
        foreach (var item in vm.State["models"]!.AsArray())
        {
            var value = item!; var identity = value["id"]!.ToString();
            models.Children.Add(ItemRow(Stack(Text(value["name"]!.ToString(), 16), Text(value["model_id"]!.ToString()), Text(string.Join(" · ", value["capabilities"]!.AsArray().Select(x => x!.ToString())))), Row(Icon(Symbol.Setting, "Edit / Duplicate profile", () => ModelDialog(value)), Icon(Symbol.Play, "Test model", () => OpenModelDebug(identity)), Icon(Symbol.Delete, "Delete model profile", async () => { if (await Confirm("Delete model profile", value["name"]!.ToString())) { await vm.Client.Delete($"api/v1/models/{identity}"); await ReloadPage(); } }))));
        }
        pivot.Items.Add(new PivotItem { Header = "Model profile", Content = new ScrollViewer { Content = models } });
        var roles = new StackPanel { Spacing = 16, MaxWidth = 760, HorizontalAlignment = HorizontalAlignment.Left };
        var bindings = new Dictionary<string, ComboBox>();
        foreach (var (id, name, capability) in new[] { ("main", "Main model", "text"), ("decision", "Quick decisions", "text"), ("code", "Code", "text"), ("reasoning", "Reasoning review", "text"), ("summary", "Conversation summary", "text"), ("vision", "Vision", "vision"), ("asr", "Speech recognition", "asr"), ("tts", "Speech output", "tts") })
        {
            var choices = vm.ModelChoices(capability);
            if (id != "main") choices = new[] { new Choice("", id is "vision" or "asr" or "tts" ? "Unconfigured" : "Inherit main model") }.Concat(choices);
            var combo = Combo(name, choices, vm.State["roles"]?["bindings"]?[id]?.ToString() ?? ""); bindings[id] = combo; roles.Children.Add(combo);
        }
        var agentBindings = new Dictionary<string, ComboBox>();
        foreach (var id in new[] { "openclaw", "opencode", "hermes" })
        {
            var combo = Combo(id + " model override", new[] { new Choice("", "Inherit the model assigned to this role") }.Concat(vm.ModelChoices("tools")), vm.State["roles"]?["agents"]?[id]?.ToString() ?? ""); agentBindings[id] = combo; roles.Children.Add(combo);
        }
        roles.Children.Add(Command("Save model assignments", Symbol.Save, async () => { await vm.Client.Put("api/v1/roles", new { bindings = bindings.ToDictionary(x => x.Key, x => string.IsNullOrEmpty(Selected(x.Value)) ? null : Selected(x.Value)), agents = agentBindings.ToDictionary(x => x.Key, x => string.IsNullOrEmpty(Selected(x.Value)) ? null : Selected(x.Value)) }); await vm.Refresh(); UpdateMainBrainStatus(); ShowInfo("Assignments saved for new tasks."); }));
        pivot.Items.Add(new PivotItem { Header = "Model assignments", Content = new ScrollViewer { Content = roles } });
        pageHost.Children.Add(pivot);
    }
    private Grid ItemRow(FrameworkElement content, FrameworkElement commands)
    {
        var grid = new Grid { Padding = new Thickness(0, 12, 0, 12), ColumnSpacing = 16, BorderThickness = new Thickness(0, 0, 0, 1), BorderBrush = (Microsoft.UI.Xaml.Media.Brush)Application.Current.Resources["DividerStrokeColorDefaultBrush"] };
        grid.ColumnDefinitions.Add(new ColumnDefinition()); grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto }); grid.Children.Add(content); Grid.SetColumn(commands, 1); grid.Children.Add(commands); return grid;
    }
    private async Task ReloadPage() { await vm.Refresh(); UpdateMainBrainStatus(); await ShowPage(page); }
    private async Task ProviderDialog(JsonNode? old = null)
    {
        if (old?["id"]?.ToString() is "bonsai" or "fish" or "firered" or "zonos3") { ShowInfo("Add your own connection or model profile to customize built-in settings."); return; }
        var name = Field("Name", old?["name"]?.ToString() ?? "");
        var protocol = Combo("Protocol", new[] { new Choice("openai", "OpenAI-compatible / Local inference"), new("anthropic", "Anthropic native"), new("gemini", "Gemini native"), new("brave", "Brave Search"), new("tavily", "Tavily"), new("searxng", "SearXNG") }, old?["protocol"]?.ToString());
        var url = Field("Endpoint URL", old?["base_url"]?.ToString() ?? "http://127.0.0.1:1234/v1");
        var key = new PasswordBox { Header = old?["has_key"]?.GetValue<bool>() == true ? "API key (leave blank to keep current key)" : "API key" };
        var clearKey = new CheckBox { Content = "Clear saved key" };
        var enabled = new CheckBox { Content = "Enable connection", IsChecked = old?["enabled"]?.GetValue<bool>() ?? true };
        protocol.SelectionChanged += (_, _) => { if (old == null) url.Text = Selected(protocol) switch { "anthropic" => "https://api.anthropic.com/v1", "gemini" => "https://generativelanguage.googleapis.com/v1beta", "brave" => "https://api.search.brave.com/res/v1", "tavily" => "https://api.tavily.com", "searxng" => "http://127.0.0.1:8888", _ => "http://127.0.0.1:1234/v1" }; };
        if (!await Form(old == null ? "Add connection" : "Edit connection", Stack(name, protocol, url, key, clearKey, enabled))) return;
        var body = new { name = name.Text, protocol = Selected(protocol), base_url = url.Text.Trim(), api_key = clearKey.IsChecked == true ? "" : string.IsNullOrEmpty(key.Password) ? null : key.Password, enabled = enabled.IsChecked == true };
        if (old == null) await vm.Client.Post("api/v1/providers", body); else await vm.Client.Put("api/v1/providers/" + old["id"], body);
        key.Password = ""; await ReloadPage();
    }
    private async Task ModelDialog(JsonNode? old = null)
    {
        if (old?["id"]?.ToString() is "fish" or "firered") { ShowInfo("To add another speech model, create a compatible audio connection. The existing local speech settings are preserved."); return; }
        var builtin = old?["id"]?.ToString() is "bonsai" or "fish" or "firered";
        var providers = vm.State["providers"]!.AsArray().Where(p => p!["protocol"]!.ToString() is "openai" or "anthropic" or "gemini").Select(p => new Choice(p!["id"]!.ToString(), p["name"]!.ToString()));
        var provider = Combo("Provider connections", providers, old?["provider_id"]?.ToString());
        if (provider.Items.Count == 0)
        {
            ShowInfo("Add a provider connection before creating a model profile.");
            return;
        }
        var name = Field("Profile name", (old?["name"]?.ToString() ?? "") + (builtin ? " / Custom" : ""));
        var modelId = Field("Model ID", old?["model_id"]?.ToString() ?? "");
        var context = Number("Context length", old?["context"]?.GetValue<double>() ?? 8192, 512, 2_000_000);
        var output = Number("Maximum output tokens", old?["max_tokens"]?.GetValue<double>() ?? 2048, 1, 131072);
        var temperature = Number("Temperature", old?["temperature"]?.GetValue<double>() ?? 0.7, 0, 2);
        var timeout = Number("Timeout (seconds)", old?["timeout"]?.GetValue<double>() ?? 240, 5, 600);
        var stream = new CheckBox { Content = "Stream output", IsChecked = old?["stream"]?.GetValue<bool>() ?? true };
        var enabled = new CheckBox { Content = "Enable profile", IsChecked = old?["enabled"]?.GetValue<bool>() ?? true };
        var capabilities = new Dictionary<string, CheckBox>(); var capabilityPanel = new StackPanel { Spacing = 4 };
        foreach (var (id, label) in new[] { ("text", "Text"), ("tools", "Tool calls"), ("vision", "Vision"), ("asr", "Speech recognition (compatible audio API)"), ("tts", "Speech synthesis (compatible audio API)") })
        {
            var check = new CheckBox { Content = label, IsChecked = old?["capabilities"]?.AsArray().Any(x => x!.ToString() == id) ?? id == "text" }; capabilities[id] = check; capabilityPanel.Children.Add(check);
        }
        var voice = Field("Voice / Voice ID", old?["voice"]?.ToString() ?? "alloy"); var speed = Number("Speech rate", old?["speed"]?.GetValue<double>() ?? 1, 0.25, 4); var language = Field("Language (leave blank for automatic detection)", old?["language"]?.ToString() ?? "");
        void Adapt()
        {
            var text = capabilities["text"].IsChecked == true;
            context.IsEnabled = output.IsEnabled = temperature.IsEnabled = stream.IsEnabled = text;
            ToolTipService.SetToolTip(temperature, text ? "Select capabilities supported by the model's API" : "Text sampling parameters do not apply to speech APIs");
            voice.IsEnabled = speed.IsEnabled = capabilities["tts"].IsChecked == true;
            language.IsEnabled = capabilities["asr"].IsChecked == true || (capabilities["tts"].IsChecked == true && Selected(provider) == "zonos3");
            if (Selected(provider) == "zonos3") speed.IsEnabled = false;
        }
        provider.SelectionChanged += (_, _) => Adapt();
        foreach (var check in capabilities.Values) { check.Checked += (_, _) => Adapt(); check.Unchecked += (_, _) => Adapt(); } Adapt();
        var discovered = Combo("Models from this endpoint", Array.Empty<Choice>()); discovered.Visibility = Visibility.Collapsed;
        discovered.SelectionChanged += (_, _) => { if (!string.IsNullOrEmpty(Selected(discovered))) modelId.Text = Selected(discovered); };
        var fetch = Command("List models", Symbol.Refresh, async () => { var value = await vm.Client.Post($"api/v1/providers/{Selected(provider)}/probe", new { generate = false }); discovered.Items.Clear(); foreach (var entry in value["models"]!.AsArray()) discovered.Items.Add(new Choice(entry!.ToString(), entry.ToString())); discovered.Visibility = Visibility.Visible; if (discovered.Items.Count > 0) discovered.SelectedIndex = 0; });
        if (!await Form(builtin ? "Duplicate built-in model profile" : old == null ? "Add model profile" : "Edit model profile", Stack(name, provider, modelId, fetch, discovered, capabilityPanel, context, output, temperature, timeout, stream, voice, speed, language, enabled), builtin ? "Create copy" : "Save")) return;
        var body = new { name = name.Text, provider_id = Selected(provider), model_id = modelId.Text.Trim(), context = (int)context.Value, max_tokens = (int)output.Value, temperature = temperature.Value, timeout = (int)timeout.Value, stream = stream.IsChecked == true, enabled = enabled.IsChecked == true, capabilities = capabilities.Where(x => x.Value.IsChecked == true).Select(x => x.Key).ToArray(), voice = voice.Text, speed = speed.Value, language = language.Text };
        if (old == null || builtin) await vm.Client.Post("api/v1/models", body); else await vm.Client.Put("api/v1/models/" + old["id"], body);
        await ReloadPage();
    }
}
