using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using System.Text.Json.Nodes;
using JuiAI.ControlCenter.ViewModels;

namespace JuiAI.ControlCenter;

public sealed partial class MainWindow
{
    private static TextBox Field(string header, string value = "", bool multi = false) => new() { Header = header, Text = value, TextWrapping = TextWrapping.Wrap, AcceptsReturn = multi, MinHeight = multi ? 140 : 0, HorizontalAlignment = HorizontalAlignment.Stretch };
    private static NumberBox Number(string header, double value, double min, double max) => new() { Header = header, Value = value, Minimum = min, Maximum = max, SpinButtonPlacementMode = NumberBoxSpinButtonPlacementMode.Compact };
    private async Task<bool> Form(string title, StackPanel content, string? primary = null) => await new ContentDialog { XamlRoot = Root.XamlRoot, RequestedTheme = ElementTheme.Dark, Title = title, PrimaryButtonText = primary ?? Locale.Text("Save"), CloseButtonText = Locale.Text("Cancel"), DefaultButton = ContentDialogButton.Close, Content = new ScrollViewer { Content = content, MaxHeight = 570, MinWidth = 390, HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled } }.ShowAsync() == ContentDialogResult.Primary;

    private void ModelsPage()
    {
        var pivot = new Pivot();
        var providers = new StackPanel { Spacing = 12 };
        providers.Children.Add(Row(Command(Locale.Text("Add connection"), Symbol.Add, () => ProviderDialog()), Command(Locale.Text("Export configuration"), Symbol.Download, async () => await Export("JuiAI-configuration", (await vm.Client.Get("api/v1/configuration/export")).ToJsonString(new() { WriteIndented = true })))));
        foreach (var item in vm.State["providers"]!.AsArray())
        {
            var value = item!; var identity = value["id"]!.ToString();
            var local = value["local"]?.GetValue<bool>() == true;
            var label = Stack(Text(value["name"]!.ToString(), 16), Text($"{value["protocol"]} · {(local ? Locale.Text("Local") : Locale.Text("External API"))} · {(value["enabled"]?.GetValue<bool>() == true ? Locale.Text("Enabled") : Locale.Text("Disabled"))}"), Text(value["base_url"]?.ToString() ?? ""));
            var commands = Row(Icon(Symbol.Setting, Locale.Text("Edit connection"), () => ProviderDialog(value)), Icon(Symbol.Link, Locale.Text("List models / Test connection"), async () =>
            {
                if (value["protocol"]!.ToString() is "brave" or "tavily" or "searxng") { ShowInfo(Locale.Text("Save a search connection on the search page before testing.")); return; }
                var result = await vm.Client.Post($"api/v1/providers/{identity}/probe", new { generate = false });
                await Form(Locale.Text("Connection result"), Stack(Text(result.ToJsonString(new() { WriteIndented = true }))), Locale.Text("Done"));
            }), Icon(Symbol.Delete, Locale.Text("Delete connection"), async () => { if (await Confirm(Locale.Text("Delete connection"), value["name"]!.ToString())) { await vm.Client.Delete($"api/v1/providers/{identity}"); await ReloadPage(); } }));
            providers.Children.Add(ItemRow(label, commands));
        }
        pivot.Items.Add(new PivotItem { Header = Locale.Text("Provider connections"), Content = new ScrollViewer { Content = providers } });
        var models = new StackPanel { Spacing = 12 }; models.Children.Add(Command(Locale.Text("Add model profile"), Symbol.Add, () => ModelDialog()));
        foreach (var item in vm.State["models"]!.AsArray())
        {
            var value = item!; var identity = value["id"]!.ToString();
            models.Children.Add(ItemRow(Stack(Text(value["name"]!.ToString(), 16), Text(value["model_id"]!.ToString()), Text(string.Join(" · ", value["capabilities"]!.AsArray().Select(x => Locale.Status(x?.ToString()))))), Row(Icon(Symbol.Setting, Locale.Text("Edit / Duplicate profile"), () => ModelDialog(value)), Icon(Symbol.Play, Locale.Text("Test model"), () => OpenModelDebug(identity)), Icon(Symbol.Delete, Locale.Text("Delete model profile"), async () => { if (await Confirm(Locale.Text("Delete model profile"), value["name"]!.ToString())) { await vm.Client.Delete($"api/v1/models/{identity}"); await ReloadPage(); } }))));
        }
        pivot.Items.Add(new PivotItem { Header = Locale.Text("Model profile"), Content = new ScrollViewer { Content = models } });
        var roles = new StackPanel { Spacing = 16, MaxWidth = 760, HorizontalAlignment = HorizontalAlignment.Left };
        var bindings = new Dictionary<string, ComboBox>();
        foreach (var (id, name, capability) in new[] { ("main", Locale.Text("Main model"), "text"), ("decision", Locale.Text("Quick decisions"), "text"), ("code", Locale.Text("Code"), "text"), ("reasoning", Locale.Text("Reasoning review"), "text"), ("summary", Locale.Text("Conversation summary"), "text"), ("vision", Locale.Text("Vision"), "vision"), ("asr", Locale.Text("Speech recognition"), "asr"), ("tts", Locale.Text("Speech output"), "tts") })
        {
            var choices = vm.ModelChoices(capability);
            if (id != "main") choices = new[] { new Choice("", id is "vision" or "asr" or "tts" ? Locale.Text("Unconfigured") : Locale.Text("Inherit main model")) }.Concat(choices);
            var combo = Combo(name, choices, vm.State["roles"]?["bindings"]?[id]?.ToString() ?? ""); bindings[id] = combo; roles.Children.Add(combo);
        }
        var agentBindings = new Dictionary<string, ComboBox>();
        foreach (var id in new[] { "openclaw", "opencode", "hermes" })
        {
            var combo = Combo(id + Locale.Text(" model override"), new[] { new Choice("", Locale.Text("Inherit the model assigned to this role")) }.Concat(vm.ModelChoices("tools")), vm.State["roles"]?["agents"]?[id]?.ToString() ?? ""); agentBindings[id] = combo; roles.Children.Add(combo);
        }
        roles.Children.Add(Command(Locale.Text("Save model assignments"), Symbol.Save, async () => { await vm.Client.Put("api/v1/roles", new { bindings = bindings.ToDictionary(x => x.Key, x => string.IsNullOrEmpty(Selected(x.Value)) ? null : Selected(x.Value)), agents = agentBindings.ToDictionary(x => x.Key, x => string.IsNullOrEmpty(Selected(x.Value)) ? null : Selected(x.Value)) }); await vm.Refresh(); UpdateMainBrainStatus(); ShowInfo(Locale.Text("Assignments saved for new tasks.")); }));
        pivot.Items.Add(new PivotItem { Header = Locale.Text("Model assignments"), Content = new ScrollViewer { Content = roles } });
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
        if (old?["id"]?.ToString() is "bonsai" or "fish" or "firered" or "zonos3") { ShowInfo(Locale.Text("Add your own connection or model profile to customize built-in settings.")); return; }
        var name = Field(Locale.Text("Name"), old?["name"]?.ToString() ?? "");
        var protocol = Combo(Locale.Text("Protocol"), new[] { new Choice("openai", Locale.Text("OpenAI-compatible / Local inference")), new("anthropic", Locale.Text("Anthropic native")), new("gemini", Locale.Text("Gemini native")), new("brave", "Brave Search"), new("tavily", "Tavily"), new("searxng", "SearXNG") }, old?["protocol"]?.ToString());
        var url = Field(Locale.Text("Endpoint URL"), old?["base_url"]?.ToString() ?? "http://127.0.0.1:1234/v1");
        var key = new PasswordBox { Header = old?["has_key"]?.GetValue<bool>() == true ? Locale.Text("API key (leave blank to keep current key)") : Locale.Text("API key") };
        var clearKey = new CheckBox { Content = Locale.Text("Clear saved key") };
        var enabled = new CheckBox { Content = Locale.Text("Enable connection"), IsChecked = old?["enabled"]?.GetValue<bool>() ?? true };
        protocol.SelectionChanged += (_, _) => { if (old == null) url.Text = Selected(protocol) switch { "anthropic" => "https://api.anthropic.com/v1", "gemini" => "https://generativelanguage.googleapis.com/v1beta", "brave" => "https://api.search.brave.com/res/v1", "tavily" => "https://api.tavily.com", "searxng" => "http://127.0.0.1:8888", _ => "http://127.0.0.1:1234/v1" }; };
        if (!await Form(old == null ? Locale.Text("Add connection") : Locale.Text("Edit connection"), Stack(name, protocol, url, key, clearKey, enabled))) return;
        var body = new { name = name.Text, protocol = Selected(protocol), base_url = url.Text.Trim(), api_key = clearKey.IsChecked == true ? "" : string.IsNullOrEmpty(key.Password) ? null : key.Password, enabled = enabled.IsChecked == true };
        if (old == null) await vm.Client.Post("api/v1/providers", body); else await vm.Client.Put("api/v1/providers/" + old["id"], body);
        key.Password = ""; await ReloadPage();
    }
    private async Task ModelDialog(JsonNode? old = null)
    {
        if (old?["id"]?.ToString() is "fish" or "firered") { ShowInfo(Locale.Text("To add another speech model, create a compatible audio connection. The existing local speech settings are preserved.")); return; }
        var builtin = old?["id"]?.ToString() is "bonsai" or "fish" or "firered";
        var providers = vm.State["providers"]!.AsArray().Where(p => p!["protocol"]!.ToString() is "openai" or "anthropic" or "gemini").Select(p => new Choice(p!["id"]!.ToString(), p["name"]!.ToString()));
        var provider = Combo(Locale.Text("Provider connections"), providers, old?["provider_id"]?.ToString());
        if (provider.Items.Count == 0)
        {
            ShowInfo(Locale.Text("Add a provider connection before creating a model profile."));
            return;
        }
        var name = Field(Locale.Text("Profile name"), (old?["name"]?.ToString() ?? "") + (builtin ? Locale.Text(" / Custom") : ""));
        var modelId = Field(Locale.Text("Model ID"), old?["model_id"]?.ToString() ?? "");
        var context = Number(Locale.Text("Context length"), old?["context"]?.GetValue<double>() ?? 8192, 512, 2_000_000);
        var output = Number(Locale.Text("Maximum output tokens"), old?["max_tokens"]?.GetValue<double>() ?? 2048, 1, 131072);
        var temperature = Number(Locale.Text("Temperature"), old?["temperature"]?.GetValue<double>() ?? 0.7, 0, 2);
        var timeout = Number(Locale.Text("Timeout (seconds)"), old?["timeout"]?.GetValue<double>() ?? 240, 5, 600);
        var stream = new CheckBox { Content = Locale.Text("Stream output"), IsChecked = old?["stream"]?.GetValue<bool>() ?? true };
        var enabled = new CheckBox { Content = Locale.Text("Enable profile"), IsChecked = old?["enabled"]?.GetValue<bool>() ?? true };
        var capabilities = new Dictionary<string, CheckBox>(); var capabilityPanel = new StackPanel { Spacing = 4 };
        foreach (var (id, label) in new[] { ("text", Locale.Text("Text")), ("tools", Locale.Text("Tool calls")), ("vision", Locale.Text("Vision")), ("asr", Locale.Text("Speech recognition (compatible audio API)")), ("tts", Locale.Text("Speech synthesis (compatible audio API)")) })
        {
            var check = new CheckBox { Content = label, IsChecked = old?["capabilities"]?.AsArray().Any(x => x!.ToString() == id) ?? id == "text" }; capabilities[id] = check; capabilityPanel.Children.Add(check);
        }
        var voice = Field(Locale.Text("Voice / Voice ID"), old?["voice"]?.ToString() ?? "alloy"); var speed = Number(Locale.Text("Speech rate"), old?["speed"]?.GetValue<double>() ?? 1, 0.25, 4); var language = Field(Locale.Text("Language (leave blank for automatic detection)"), old?["language"]?.ToString() ?? "");
        void Adapt()
        {
            var text = capabilities["text"].IsChecked == true;
            context.IsEnabled = output.IsEnabled = temperature.IsEnabled = stream.IsEnabled = text;
            ToolTipService.SetToolTip(temperature, text ? Locale.Text("Select capabilities supported by the model's API") : Locale.Text("Text sampling parameters do not apply to speech APIs"));
            voice.IsEnabled = speed.IsEnabled = capabilities["tts"].IsChecked == true;
            language.IsEnabled = capabilities["asr"].IsChecked == true || (capabilities["tts"].IsChecked == true && Selected(provider) == "zonos3");
            if (Selected(provider) == "zonos3") speed.IsEnabled = false;
        }
        provider.SelectionChanged += (_, _) => Adapt();
        foreach (var check in capabilities.Values) { check.Checked += (_, _) => Adapt(); check.Unchecked += (_, _) => Adapt(); } Adapt();
        var discovered = Combo(Locale.Text("Models from this endpoint"), Array.Empty<Choice>()); discovered.Visibility = Visibility.Collapsed;
        discovered.SelectionChanged += (_, _) => { if (!string.IsNullOrEmpty(Selected(discovered))) modelId.Text = Selected(discovered); };
        var fetch = Command(Locale.Text("List models"), Symbol.Refresh, async () => { var value = await vm.Client.Post($"api/v1/providers/{Selected(provider)}/probe", new { generate = false }); discovered.Items.Clear(); foreach (var entry in value["models"]!.AsArray()) discovered.Items.Add(new Choice(entry!.ToString(), entry.ToString())); discovered.Visibility = Visibility.Visible; if (discovered.Items.Count > 0) discovered.SelectedIndex = 0; });
        if (!await Form(builtin ? Locale.Text("Duplicate built-in model profile") : old == null ? Locale.Text("Add model profile") : Locale.Text("Edit model profile"), Stack(name, provider, modelId, fetch, discovered, capabilityPanel, context, output, temperature, timeout, stream, voice, speed, language, enabled), builtin ? Locale.Text("Create copy") : Locale.Text("Save"))) return;
        var body = new { name = name.Text, provider_id = Selected(provider), model_id = modelId.Text.Trim(), context = (int)context.Value, max_tokens = (int)output.Value, temperature = temperature.Value, timeout = (int)timeout.Value, stream = stream.IsChecked == true, enabled = enabled.IsChecked == true, capabilities = capabilities.Where(x => x.Value.IsChecked == true).Select(x => x.Key).ToArray(), voice = voice.Text, speed = speed.Value, language = language.Text };
        if (old == null || builtin) await vm.Client.Post("api/v1/models", body); else await vm.Client.Put("api/v1/models/" + old["id"], body);
        await ReloadPage();
    }
}
