using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using System.Text.Json.Nodes;
using JuiAI.ControlCenter.Controls;
using JuiAI.ControlCenter.ViewModels;
using Windows.ApplicationModel.DataTransfer;
using Windows.Storage.Pickers;
using WinRT.Interop;

namespace JuiAI.ControlCenter;

public sealed partial class MainWindow
{
    private StackPanel? messages;
    private StackPanel? trace;
    private TextBox? input;
    private ComboBox? mode;
    private ComboBox? chatModel;
    private CheckBox? useMemory;
    private CheckBox? useSearch;
    private TextBlock? liveText;
    private TextBlock? attachment;
    private TextBlock? chatWorkspace;
    private TextBlock? chatMainBrain;
    private string? attachedImage;
    private string liveRun = "";
    private ScrollViewer? chatScroll;
    private DispatcherTimer? recordTimer;

    private async Task ChatPage()
    {
        var grid = new Grid { ColumnSpacing = 16 };
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(210) });
        grid.ColumnDefinitions.Add(new ColumnDefinition());
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(270) });
        var history = new ListView { ItemsSource = vm.Conversations, SelectionMode = ListViewSelectionMode.Single, HorizontalContentAlignment = HorizontalAlignment.Stretch };
        history.SelectionChanged += (_, _) => { if (history.SelectedItem is Choice choice) Run(async () => { vm.ConversationId = choice.Id; await LoadConversation(); }); };
        var left = new Grid { RowSpacing = 12 }; left.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto }); left.RowDefinitions.Add(new RowDefinition());
        left.Children.Add(Command(Locale.Text("New conversation"), Symbol.Add, NewConversation)); Grid.SetRow(history, 1); left.Children.Add(history); grid.Children.Add(left);
        var center = new Grid { RowSpacing = 12 }; center.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto }); center.RowDefinitions.Add(new RowDefinition()); center.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto }); Grid.SetColumn(center, 1); grid.Children.Add(center);
        mode = Combo(Locale.Text("Routing"), new[] { new Choice("auto", Locale.Text("Automatic routing")), new("chat", Locale.Text("Chat with Jui only")), new("general", "OpenClaw"), new("code", "OpenCode"), new("research", "Hermes"), new("pipeline", Locale.Text("Coordinate three agents")) });
        chatModel = Combo(Locale.Text("Main model for this message"), new[] { new Choice("", Locale.Text("Use the default main model")) }.Concat(vm.ModelChoices("text")));
        Microsoft.UI.Xaml.Automation.AutomationProperties.SetAutomationId(chatModel, "ChatTurnBrain");
        ToolTipService.SetToolTip(chatModel, Locale.Text("Applies only to the next message"));
        var toolbar = new Grid { ColumnSpacing = 12 }; toolbar.ColumnDefinitions.Add(new ColumnDefinition()); toolbar.ColumnDefinitions.Add(new ColumnDefinition()); toolbar.Children.Add(mode); Grid.SetColumn(chatModel, 1); toolbar.Children.Add(chatModel);
        chatWorkspace = Text(Locale.Text("Workspace · ") + ControlViewModel.WorkspaceName(vm.State["workspaces"]!.AsArray().FirstOrDefault(w => w!["id"]!.ToString() == vm.WorkspaceId), vm.WorkspaceId), 12);
        chatMainBrain = Text(Locale.Text("Default main model · ") + vm.MainModelName);
        Microsoft.UI.Xaml.Automation.AutomationProperties.SetAutomationId(chatMainBrain, "ChatDefaultBrain");
        var brainSwitch = Command(Locale.Text("Switch main model"), Symbol.Switch, PickDefaultMainBrain);
        Microsoft.UI.Xaml.Automation.AutomationProperties.SetAutomationId(brainSwitch, "ChatSwitchDefaultBrain");
        var workspaceHeader = new Grid { ColumnSpacing = 12 };
        workspaceHeader.ColumnDefinitions.Add(new ColumnDefinition());
        workspaceHeader.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        workspaceHeader.Children.Add(new StackPanel { Spacing = 4, Children = { chatWorkspace, chatMainBrain } });
        Grid.SetColumn(brainSwitch, 1);
        workspaceHeader.Children.Add(brainSwitch);
        center.Children.Add(Stack(workspaceHeader, toolbar));
        messages = new StackPanel { Spacing = 24, Padding = new Thickness(4, 10, 10, 16) };
        chatScroll = new ScrollViewer { Content = messages, HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled }; Grid.SetRow(chatScroll, 1); center.Children.Add(chatScroll);
        input = new TextBox { AcceptsReturn = true, TextWrapping = TextWrapping.Wrap, MinHeight = 86, MaxHeight = 180, PlaceholderText = Locale.Text("Message Jui"), HorizontalAlignment = HorizontalAlignment.Stretch };
        Microsoft.UI.Xaml.Automation.AutomationProperties.SetAutomationId(input, "ChatInput");
        useMemory = new CheckBox { Content = Locale.Text("Use memory"), IsChecked = true }; useSearch = new CheckBox { Content = Locale.Text("Search the web"), IsChecked = false };
        attachment = Text("");
        var commands = new Grid(); commands.ColumnDefinitions.Add(new ColumnDefinition()); commands.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        commands.Children.Add(Row(useMemory, useSearch));
        var buttons = Row(Icon(Symbol.Attach, Locale.Text("Add an image"), AttachImage), Icon(Symbol.Microphone, Locale.Text("Start or stop recording"), ToggleRecord), Icon(Symbol.Mute, Locale.Text("Stop reading aloud"), () => { StopSpeech(); return Task.CompletedTask; }), Icon(Symbol.Stop, Locale.Text("Stop the current task"), CancelCurrent), Icon(Symbol.Send, Locale.Text("Send"), Send)); Grid.SetColumn(buttons, 1); commands.Children.Add(buttons);
        var composer = Stack(attachment, input, commands); Grid.SetRow(composer, 2); center.Children.Add(composer);
        trace = new StackPanel { Spacing = 10 };
        var right = new ScrollViewer { Content = Stack(Text(Locale.Text("Tasks and approvals"), 16), trace), HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled }; Grid.SetColumn(right, 2); grid.Children.Add(right);
        void Adapt()
        {
            var width = pageHost.ActualWidth;
            left.Visibility = width < 1020 ? Visibility.Collapsed : Visibility.Visible;
            grid.ColumnDefinitions[0].Width = width < 1020 ? new GridLength(0) : new GridLength(210);
            right.Visibility = width < 800 ? Visibility.Collapsed : Visibility.Visible;
            grid.ColumnDefinitions[2].Width = width < 800 ? new GridLength(0) : new GridLength(270);
        }
        grid.SizeChanged += (_, _) => Adapt();
        pageHost.Children.Add(grid);
        if (vm.ConversationId != null) await LoadConversation();
        else if (string.IsNullOrEmpty(vm.MainModelId))
            messages.Children.Add(Stack(Text(Locale.Text("Connect your first model"), 22),
                Text(Locale.Text("Add a provider connection, create a text model profile, then select it as your main model.")),
                Command(Locale.Text("Models and connections"), Symbol.Library, () => ShowPage("models"))));
        else messages.Children.Add(Text("Jui", 24));
        await RefreshPendingApprovals();
    }

    private async Task PickDefaultMainBrain()
    {
        var choices = vm.ModelChoices("text").ToArray();
        if (choices.Length == 0)
        {
            ShowInfo(Locale.Text("Enable a text model in Models and connections."));
            return;
        }
        var picker = Combo(Locale.Text("Text model"), choices, vm.MainModelId);
        Microsoft.UI.Xaml.Automation.AutomationProperties.SetAutomationId(picker, "DefaultMainBrainPicker");
        if (!await Form(Locale.Text("Change default main model"), Stack(picker), Locale.Text("Set as default"))) return;
        var modelId = Selected(picker);
        if (modelId == vm.MainModelId) return;

        await vm.SetMainModel(modelId);
        UpdateMainBrainStatus();
        ShowInfo(Locale.Format("Set {0} as the default main model.", vm.MainModelName));
    }

    private async Task NewConversation()
    {
        var value = await vm.Client.Post("api/v1/conversations", new { workspace_id = vm.WorkspaceId });
        vm.ConversationId = value["id"]!.ToString(); vm.RunId = null; attachedImage = null;
        await vm.RefreshConversations(); await LoadConversation();
    }
    private async Task LoadConversation()
    {
        if (vm.ConversationId == null || messages == null) return;
        currentConversation = await vm.Client.Get("api/v1/conversations/" + vm.ConversationId);
        var workspace = vm.State["workspaces"]!.AsArray().FirstOrDefault(w => w!["id"]!.ToString() == currentConversation["workspace_id"]!.ToString());
        if (chatWorkspace != null) { chatWorkspace.Text = Locale.Text("Workspace · ") + ControlViewModel.WorkspaceName(workspace, currentConversation["workspace_id"]!.ToString()); ToolTipService.SetToolTip(chatWorkspace, workspace?["path"]?.ToString() ?? ""); }
        messages.Children.Clear(); liveText = null;
        foreach (var item in currentConversation["messages"]!.AsArray().Where(m => m?["superseded"]?.GetValue<bool>() != true)) AddMessage(item!);
        var running = currentConversation["runs"]!.AsArray().LastOrDefault(r => r?["status"]?.ToString() is "queued" or "running");
        vm.RunId = running?["id"]?.ToString();
        if (running != null) { liveRun = vm.RunId!; liveText = Text(running["output"]?.ToString() ?? "", 15); messages.Children.Add(Stack(Text(Locale.Text("Jui · Working"), 12), liveText)); }
        chatScroll?.ChangeView(null, double.MaxValue, null);
    }
    private void AddMessage(JsonNode item)
    {
        if (messages == null) return;
        var assistant = item["role"]!.ToString() == "assistant";
        var content = item["content"]?.ToString() ?? "";
        var block = Stack(Text(assistant ? "Jui" : Locale.Text("You"), 12), MarkdownView.Render(content));
        if (assistant)
            block.Children.Add(Row(Icon(Symbol.Copy, Locale.Text("Copy reply"), () => { var data = new DataPackage(); data.SetText(content); Clipboard.SetContent(data); return Task.CompletedTask; }), Icon(Symbol.Volume, Locale.Text("Read reply aloud"), () => Speak(content, item["run_id"]?.ToString())), Icon(Symbol.Save, Locale.Text("Save to memory"), () => SaveMemoryFromText(content)), Icon(Symbol.Refresh, Locale.Text("Regenerate"), () => Regenerate(item))));
        messages.Children.Add(block);
    }
    private async Task Send()
    {
        if (input == null || string.IsNullOrWhiteSpace(input.Text)) return;
        StopSpeech();
        if (vm.ConversationId == null) await NewConversation();
        var prompt = input.Text;
        var modelPicker = chatModel!;
        var modelRef = Selected(modelPicker);
        var result = await vm.Client.Post($"api/v1/conversations/{vm.ConversationId}/messages", new { prompt, mode = Selected(mode!), model_ref = string.IsNullOrEmpty(modelRef) ? null : modelRef, use_memory = useMemory!.IsChecked == true, use_search = useSearch!.IsChecked == true, image = attachedImage });
        if (Selected(modelPicker) == modelRef) modelPicker.SelectedIndex = 0;
        vm.RunId = result["id"]!.ToString(); liveRun = vm.RunId; input.Text = ""; attachedImage = null; attachment!.Text = "";
        await vm.RefreshConversations(); await LoadConversation();
    }
    private async Task Regenerate(JsonNode item)
    {
        var original = currentConversation?["messages"]?.AsArray().LastOrDefault(m => m?["role"]?.ToString() == "user" && m["run_id"]?.ToString() == item["run_id"]?.ToString());
        if (original == null) return;
        var result = await vm.Client.Post($"api/v1/conversations/{vm.ConversationId}/messages", new { prompt = original["content"]!.ToString(), mode = Selected(mode!), use_memory = useMemory!.IsChecked == true, regenerate_run_id = item["run_id"]!.ToString() });
        vm.RunId = result["id"]!.ToString(); liveRun = vm.RunId; await LoadConversation();
    }
    private async Task CancelCurrent()
    {
        StopSpeech(); if (vm.RunId != null) await vm.Client.Post($"api/v1/runs/{vm.RunId}/cancel");
    }
    private async Task AttachImage()
    {
        var picker = new FileOpenPicker(); picker.FileTypeFilter.Add(".png"); picker.FileTypeFilter.Add(".jpg"); picker.FileTypeFilter.Add(".jpeg"); InitializeWithWindow.Initialize(picker, WindowNative.GetWindowHandle(this));
        var file = await picker.PickSingleFileAsync(); if (file == null) return;
        var bytes = await File.ReadAllBytesAsync(file.Path);
        if (bytes.Length > 2_900_000) throw new InvalidOperationException(Locale.Text("Images must be 2.9 MB or smaller."));
        attachedImage = "data:image/" + (file.FileType.ToLowerInvariant() == ".png" ? "png" : "jpeg") + ";base64," + Convert.ToBase64String(bytes);
        attachment!.Text = file.Name;
    }
    private async Task ToggleRecord()
    {
        StopSpeech();
        if (!audio.Recording)
        {
            audio.Start(inputDevice); statusText.Text = Locale.Text("Recording");
            recordTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(39) };
            recordTimer.Tick += (_, _) => { recordTimer.Stop(); if (audio.Recording) Run(ToggleRecord); }; recordTimer.Start(); return;
        }
        recordTimer?.Stop();
        var wav = await audio.Stop(); statusText.Text = Locale.Text("Transcribing");
        try { var result = await vm.Client.Transcribe(wav, currentConversation?["workspace_id"]?.ToString() ?? vm.WorkspaceId, lifetime.Token); if (input != null) input.Text = result["text"]!.ToString(); }
        finally { UpdateMainBrainStatus(updateTransientStatus: true); }
    }
    private void StopSpeech() { speech.Cancel(); speech.Dispose(); speech = new CancellationTokenSource(); audio.StopPlayback(); }
    private async Task Speak(string text, string? runId = null)
    {
        StopSpeech(); var token = speech.Token;
        var result = await vm.Client.Speak(new { text = text[..Math.Min(600, text.Length)], workspace_id = currentConversation?["workspace_id"]?.ToString() ?? vm.WorkspaceId, source_run_id = runId }, token);
        if (!token.IsCancellationRequested) audio.Play(result, outputDevice);
    }
    private async Task OnEvent(JsonNode value)
    {
        var kind = value["kind"]!.ToString(); var data = value["data"]!;
        if (kind == "voice_processing") { await OnVoiceProcessingEvent(data); return; }
        var runId = data["run_id"]?.ToString();
        if (kind == "delta" && page == "chat" && runId == liveRun)
        {
            if (liveText == null) { liveText = Text(""); messages?.Children.Add(Stack(Text("Jui", 12), liveText)); }
            liveText.Text += data["text"]?.ToString(); return;
        }
        if (kind == "stage" && page == "chat" && runId == vm.RunId)
        {
            var stage = data["stage"]?.ToString();
            var status = Locale.Status(data["status"]?.ToString());
            var label = stage == null ? status : $"{Locale.Status(stage)} · {status}";
            trace?.Children.Add(Text($"{data["agent"]} · {label}"));
        }
        if (kind is "approval" or "approval_changed")
        {
            if (page == "chat") await RefreshPendingApprovals();
            else if (page == "tools") await ShowPage("tools");
            if (kind == "approval") ShowInfo(Locale.Text("An action is awaiting approval. Open Tools and approvals to review it."));
        }
        if (kind == "run" && data["status"]?.ToString() is "completed" or "failed" or "cancelled")
        {
            var wasActive = runId != null && runId == vm.RunId;
            if (wasActive)
            {
                if (data["error"] != null) ShowError(data["error"]!.ToString());
                vm.RunId = null;
                if (page == "chat") await LoadConversation();
                await vm.RefreshConversations();
            }
            if (wasActive && data["status"]?.ToString() == "completed" && vm.State["preferences"]!.AsArray().First(p => p!["id"]!.ToString() == "voice")?["auto_speak"]?.GetValue<bool>() == true && page == "chat" && currentConversation?["messages"]?.AsArray().LastOrDefault() is { } latest && latest["role"]?.ToString() == "assistant")
                Run(() => Speak(latest["content"]!.ToString(), latest["run_id"]?.ToString()));
        }
        if (kind == "update" && page == "updates") await UpdatesPage(true);
    }
    private async Task RefreshPendingApprovals()
    {
        if (page != "chat" || trace == null) return;
        var approvals = (await vm.Client.Get("api/v1/approvals")).AsArray();
        foreach (var child in trace.Children.OfType<FrameworkElement>().Where(x => x.Tag is string s && s.StartsWith("approval-")).ToList()) trace.Children.Remove(child);
        foreach (var item in approvals.Where(a => a?["status"]?.ToString() == "pending"))
        {
            var value = item!;
            var panel = ApprovalPanel(value); panel.Tag = "approval-" + value["id"]; trace.Children.Add(panel);
        }
    }
}
