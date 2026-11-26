using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Windowing;
using System.Text.Json.Nodes;
using JuiAI.ControlCenter.Services;
using JuiAI.ControlCenter.ViewModels;
using Windows.ApplicationModel.DataTransfer;
using Windows.Storage.Pickers;
using WinRT.Interop;

namespace JuiAI.ControlCenter;

public sealed partial class MainWindow : Window
{
    private readonly ControlViewModel vm = new();
    private readonly AudioService audio = new();
    private readonly CancellationTokenSource lifetime = new();
    private CancellationTokenSource speech = new();
    private readonly NavigationView navigation = new() { PaneDisplayMode = NavigationViewPaneDisplayMode.Auto, OpenPaneLength = 245, IsBackButtonVisible = NavigationViewBackButtonVisible.Collapsed, IsSettingsVisible = false, IsPaneOpen = true };
    private readonly Grid pageHost = new();
    private readonly InfoBar notice = new() { IsOpen = false, IsClosable = true };
    private readonly TextBlock statusText = new() { Text = Locale.Text("Connecting"), FontSize = 12, TextWrapping = TextWrapping.Wrap, Margin = new Thickness(12) };
    private readonly TextBlock pageTitle = new() { FontSize = 22, FontWeight = Microsoft.UI.Text.FontWeights.SemiBold };
    private readonly TrayIcon tray;
    private bool ready;
    private bool exiting;
    private bool closing;
    private bool syncingNavigation;
    private string page = "chat";
    private string desiredPage = "chat";
    private readonly SemaphoreSlim navigationLock = new(1, 1);
    private Task? eventPump;
    private int inputDevice;
    private int outputDevice = -1;
    private JsonNode? currentConversation;

    private readonly Action refreshShellLanguage;

    public MainWindow()
    {
        InitializeComponent();
        AppWindow.Resize(new Windows.Graphics.SizeInt32(1360, 900));
        if (AppWindow.Presenter is OverlappedPresenter presenter) { presenter.PreferredMinimumWidth = 960; presenter.PreferredMinimumHeight = 640; }
        var iconPath = Path.Combine(AppContext.BaseDirectory, "Assets", "JuiAI-Terminal.ico");
        AppWindow.SetIcon(iconPath);
        tray = new TrayIcon(WindowNative.GetWindowHandle(this), iconPath, () => { AppWindow.Show(); Activate(); }, () => Run(CloseRequested));
        AppWindow.Closing += (_, e) => { if (!exiting) { e.Cancel = true; Run(CloseRequested); } };
        var layout = new Grid { RowSpacing = 12, Padding = new Thickness(20, 16, 20, 16) };
        layout.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        layout.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        layout.RowDefinitions.Add(new RowDefinition());
        var header = new Grid(); header.ColumnDefinitions.Add(new ColumnDefinition()); header.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        header.Children.Add(pageTitle);
        var refresh = Icon(Symbol.Refresh, Locale.Text("Refresh"), ReloadPage);
        var newConversationButton = Icon(Symbol.Add, Locale.Text("New conversation"), async () => { await NewConversation(); navigation.SelectedItem = navigation.MenuItems[0]; await ShowPage("chat"); });
        var headerCommands = Row(newConversationButton, refresh);
        Grid.SetColumn(headerCommands, 1); header.Children.Add(headerCommands);
        layout.Children.Add(header); Grid.SetRow(notice, 1); layout.Children.Add(notice); Grid.SetRow(pageHost, 2); layout.Children.Add(pageHost);
        navigation.Content = layout;
        navigation.PaneTitle = "JuiAI Terminal";
        refreshShellLanguage = () =>
        {
            Root.Language = Locale.Language;
            SetButtonLabel(refresh, Locale.Text("Refresh"));
            SetButtonLabel(newConversationButton, Locale.Text("New conversation"));
            syncingNavigation = true;
            try
            {
                navigation.MenuItems.Clear();
                foreach (var (id, label, symbol) in new[] { ("chat", Locale.Text("Chat"), Symbol.Message), ("history", Locale.Text("Conversation history"), Symbol.Clock), ("agents", Locale.Text("Agents"), Symbol.People), ("models", Locale.Text("Models and connections"), Symbol.Library), ("debug", Locale.Text("Test model"), Symbol.Play), ("workspace", Locale.Text("Workspace"), Symbol.Folder), ("memory", Locale.Text("Memory"), Symbol.Bookmarks), ("voice", Locale.Text("Search and voice"), Symbol.Microphone), ("voice-processing", Locale.Text("Audio processing"), Symbol.Audio), ("tools", Locale.Text("Tools and approvals"), Symbol.Permissions), ("updates", Locale.Text("Check for updates"), Symbol.Sync), ("settings", Locale.Text("Settings and diagnostics"), Symbol.Setting) })
                {
                    var item = new NavigationViewItem { Content = label, Tag = id, Icon = new SymbolIcon(symbol) };
                    Microsoft.UI.Xaml.Automation.AutomationProperties.SetAutomationId(item, "Nav-" + id);
                    navigation.MenuItems.Add(item);
                }
            }
            finally { syncingNavigation = false; }
        };
        refreshShellLanguage();
        navigation.PaneFooter = statusText;
        navigation.DisplayModeChanged += (_, e) => statusText.Visibility = e.DisplayMode == NavigationViewDisplayMode.Expanded ? Visibility.Visible : Visibility.Collapsed;
        navigation.SelectionChanged += (_, e) => { if (ready && !syncingNavigation && e.SelectedItemContainer?.Tag is string id) Run(() => ShowPage(id)); };
        Root.Children.Add(navigation);
        navigation.PaneOpened += (_, _) => UpdatePaneButtonLabel();
        navigation.PaneClosed += (_, _) => UpdatePaneButtonLabel();
        Root.Loaded += (_, _) => { UpdatePaneButtonLabel(); Run(Initialize); };
        Closed += (_, _) => { lifetime.Cancel(); speech.Cancel(); debugCancellation?.Cancel(); audio.Dispose(); tray.Dispose(); vm.Client.Dispose(); };
    }

    private async Task Initialize()
    {
        if (ready) return;
        await vm.Client.Connect();
        await vm.Refresh(); await vm.RefreshConversations();
        ready = true;
        UpdateMainBrainStatus();
        navigation.SelectedItem = navigation.MenuItems[0];
        await ShowPage("chat");
        eventPump = vm.Client.Events(value =>
        {
            DispatcherQueue.TryEnqueue(() => Run(() => OnEvent(value)));
            return Task.CompletedTask;
        }, lifetime.Token, vm.State["event_cursor"]?.GetValue<long>() ?? 0);
        if (Environment.GetCommandLineArgs().Contains("--ui-smoke")) await UiSmoke();
        else if (Environment.GetCommandLineArgs().Contains("--debug")) await ShowPage("debug");
    }

    private void UpdateMainBrainStatus(bool updateTransientStatus = false)
    {
        if (updateTransientStatus || (statusText.Text != Locale.Text("Recording") && statusText.Text != Locale.Text("Transcribing")))
            statusText.Text = Locale.Text("Main model · ") + vm.MainModelName;
        if (chatMainBrain != null) chatMainBrain.Text = Locale.Text("Default main model · ") + vm.MainModelName;
    }

    private async Task ShowPage(string id)
    {
        desiredPage = id;
        await navigationLock.WaitAsync();
        try
        {
            if (id != desiredPage) return;
            if (page == "voice-processing")
            {
                renderVoiceProcessing = null;
                if (id != page) audio.StopPlayback();
            }
            if (page == "debug")
            {
                await CancelDebug();
                debugSpeechSmoke = null;
                debugAsrSmoke = null;
                debugTextSmoke = null;
                debugCompareSmoke = null;
            }
            page = id; pageHost.Children.Clear();
            var selectedPage = navigation.MenuItems.Cast<NavigationViewItem>().First(x => (string)x.Tag == id);
            syncingNavigation = true;
            try { navigation.SelectedItem = selectedPage; }
            finally { syncingNavigation = false; }
            pageTitle.Text = selectedPage.Content.ToString();
            switch (id)
            {
                case "chat": await ChatPage(); break;
                case "history": await HistoryPage(); break;
                case "agents": await AgentsPage(); break;
                case "models": ModelsPage(); break;
                case "debug": DebugPage(); break;
                case "workspace": await WorkspacePage(); break;
                case "memory": MemoryPage(); break;
                case "voice": VoicePage(); break;
                case "voice-processing": await VoiceProcessingPage(); break;
                case "tools": await ToolsPage(); break;
                case "updates": await UpdatesPage(); break;
                case "settings": SettingsPage(); break;
            }
        }
        finally { navigationLock.Release(); }
    }

    private async void Run(Func<Task> action)
    {
        try { await action(); }
        catch (OperationCanceledException) { }
        catch (Exception error) { ShowError(error.Message); }
    }
    private void ShowError(string text) { notice.Title = Locale.Text("Action incomplete"); notice.Message = text; notice.Severity = InfoBarSeverity.Error; notice.IsOpen = true; }
    private void ShowInfo(string text) { notice.Title = "JuiAI Terminal"; notice.Message = text; notice.Severity = InfoBarSeverity.Informational; notice.IsOpen = true; }
    private void UpdatePaneButtonLabel()
    {
        if (Microsoft.UI.Xaml.Media.VisualTreeHelper.GetChildrenCount(navigation) == 0) return;
        var templateRoot = Microsoft.UI.Xaml.Media.VisualTreeHelper.GetChild(navigation, 0) as FrameworkElement;
        if (templateRoot?.FindName("TogglePaneButton") is Button button)
            SetButtonLabel(button, Locale.Text(navigation.IsPaneOpen ? "Close navigation" : "Open navigation"));
    }

    private static void SetButtonLabel(Button button, string label)
    {
        ToolTipService.SetToolTip(button, label);
        Microsoft.UI.Xaml.Automation.AutomationProperties.SetName(button, label);
    }

    private async Task SetLanguage(string language)
    {
        Locale.Save(vm.Client.Root, language);
        Microsoft.Windows.Globalization.ApplicationLanguages.PrimaryLanguageOverride = language;
        vm.Client.RefreshEventLanguage();
        UpdatePaneButtonLabel();
        notice.IsOpen = false;
        refreshShellLanguage();
        await vm.Refresh();
        await vm.RefreshConversations();
        UpdateMainBrainStatus(true);
        await ShowPage(page);
    }

    private Button Icon(Symbol symbol, string label, Func<Task> action)
    {
        var button = new Button { Content = new SymbolIcon(symbol), VerticalAlignment = VerticalAlignment.Center };
        ToolTipService.SetToolTip(button, label); Microsoft.UI.Xaml.Automation.AutomationProperties.SetName(button, label);
        button.Click += (_, _) => Run(async () => { button.IsEnabled = false; try { await action(); } finally { button.IsEnabled = true; } });
        return button;
    }
    private Button Command(string label, Symbol symbol, Func<Task> action)
    {
        var button = Icon(symbol, label, action);
        var content = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 }; content.Children.Add(new SymbolIcon(symbol)); content.Children.Add(new TextBlock { Text = label }); button.Content = content; return button;
    }
    private static TextBlock Text(string value, int size = 14) => new() { Text = value, FontSize = size, TextWrapping = TextWrapping.Wrap, IsTextSelectionEnabled = true };
    private static StackPanel Stack(params UIElement[] children)
    {
        var panel = new StackPanel { Spacing = 12 };
        foreach (var child in children) panel.Children.Add(child);
        return panel;
    }
    private static StackPanel Row(params UIElement[] children)
    {
        var row = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
        foreach (var child in children) row.Children.Add(child);
        return row;
    }
    private void Scrolled(UIElement content) => pageHost.Children.Add(new ScrollViewer { Content = content, HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled, Padding = new Thickness(0, 0, 12, 16) });
    private static ComboBox Combo(string header, IEnumerable<Choice> items, string? selected = null)
    {
        var combo = new ComboBox { Header = header, MinWidth = 170, HorizontalAlignment = HorizontalAlignment.Stretch };
        foreach (var item in items) combo.Items.Add(item);
        combo.SelectedItem = combo.Items.Cast<Choice>().FirstOrDefault(c => c.Id == selected) ?? combo.Items.Cast<Choice>().FirstOrDefault();
        return combo;
    }
    private static string Selected(ComboBox combo) => (combo.SelectedItem as Choice)?.Id ?? "";
    private async Task<bool> Confirm(string title, string text)
    {
        return await new ContentDialog { XamlRoot = Root.XamlRoot, RequestedTheme = ElementTheme.Dark, Title = title, Content = new ScrollViewer { Content = Text(text), MaxHeight = 420 }, PrimaryButtonText = Locale.Text("Confirm"), CloseButtonText = Locale.Text("Cancel"), DefaultButton = ContentDialogButton.Close }.ShowAsync() == ContentDialogResult.Primary;
    }
    private async Task<string?> PickFolder()
    {
        var picker = new FolderPicker(); picker.FileTypeFilter.Add("*"); InitializeWithWindow.Initialize(picker, WindowNative.GetWindowHandle(this)); return (await picker.PickSingleFolderAsync())?.Path;
    }
    private async Task Export(string name, string content)
    {
        var picker = new FileSavePicker { SuggestedFileName = name }; picker.FileTypeChoices.Add("JSON", new List<string> { ".json" }); InitializeWithWindow.Initialize(picker, WindowNative.GetWindowHandle(this));
        var file = await picker.PickSaveFileAsync(); if (file != null) await Windows.Storage.FileIO.WriteTextAsync(file, content);
    }
    private async Task CloseRequested()
    {
        if (closing) return; closing = true;
        try
        {
            if (ready)
            {
                await vm.Refresh();
                if (vm.State["maintenance"]?.GetValue<bool>() == true) { ShowInfo(Locale.Text("An update is still running. Wait until it finishes before exiting.")); return; }
                var processing = vm.State["voice_processing_running"]?.AsArray() ?? new JsonArray();
                if (voiceProcessingSubmitting) { ShowInfo(Locale.Text("An audio processing task is being submitted. Please wait before exiting.")); return; }
                if (vm.State["running"]!.AsArray().Count > 0 || vm.State["debug_running"]?.AsArray().Count > 0 || debugRecording || processing.Count > 0)
                {
                    var result = await new ContentDialog { XamlRoot = Root.XamlRoot, RequestedTheme = ElementTheme.Dark, Title = Locale.Text("A task is still running"), PrimaryButtonText = Locale.Text("Continue in background"), SecondaryButtonText = Locale.Text("Cancel task and exit"), CloseButtonText = Locale.Text("Back"), DefaultButton = ContentDialogButton.Close }.ShowAsync();
                    if (result == ContentDialogResult.Primary) { tray.Show(); AppWindow.Hide(); return; }
                    if (result != ContentDialogResult.Secondary) return;
                    foreach (var id in vm.State["running"]!.AsArray()) await vm.Client.Post($"api/v1/runs/{id}/cancel");
                    await CancelDebug();
                    foreach (var id in vm.State["debug_running"]?.AsArray() ?? new JsonArray()) await vm.Client.Post($"api/v1/debug/{id}/cancel");
                    foreach (var id in processing) await vm.Client.Post($"{VoiceProcessingApi}/jobs/{id}/cancel");
                }
                if (vm.Client.OwnsBackend) await vm.Client.Post("api/v1/shutdown");
            }
            exiting = true; Close();
        }
        finally { closing = false; }
    }
}
