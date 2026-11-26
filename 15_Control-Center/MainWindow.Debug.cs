using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using System.Diagnostics;
using System.Text.Json.Nodes;
using JuiAI.ControlCenter.Services;
using JuiAI.ControlCenter.ViewModels;
using Windows.ApplicationModel.DataTransfer;
using Windows.Storage.Pickers;
using WinRT.Interop;

namespace JuiAI.ControlCenter;

public sealed partial class MainWindow
{
    private string? debugPreset;
    private string? debugRequestId;
    private CancellationTokenSource? debugCancellation;
    private DispatcherTimer? debugRecordTimer;
    private bool debugRecording;
    private Task<byte[]>? debugRecordingStop;
    private int debugRevision;
    private sealed record SpeechSample(byte[] Wav, string Model, string Text, string Voice, string Language, double Seconds);
    private Func<string, Task<(JsonObject Result, byte[]? Audio)>>? debugSpeechSmoke;
    private Func<byte[], Task<JsonObject>>? debugAsrSmoke;
    private Func<string, string, Task<JsonObject>>? debugTextSmoke;
    private Func<Task<JsonObject>>? debugCompareSmoke;

    private async Task OpenModelDebug(string identity)
    {
        debugPreset = identity;
        await ShowPage("debug");
    }

    private async Task CancelDebug()
    {
        ++debugRevision;
        var id = debugRequestId;
        debugRecordTimer?.Stop();
        debugCancellation?.Cancel();
        StopSpeech();
        if (debugRecording) await FinishDebugRecording();
        else if (debugRecordingStop is { } stopping) await stopping;
        if (id != null)
        {
            await vm.Client.Post($"api/v1/debug/{id}/cancel", seconds: 5);
        }
    }

    private async Task<byte[]> FinishDebugRecording()
    {
        debugRecordTimer?.Stop();
        debugRecording = false;
        var stopped = audio.Stop();
        debugRecordingStop = stopped;
        try { return await stopped; }
        finally { debugRecordingStop = null; }
    }

    private (string Id, CancellationTokenSource Cancellation) BeginDebug()
    {
        var id = Guid.NewGuid().ToString("N");
        var cancellation = CancellationTokenSource.CreateLinkedTokenSource(lifetime.Token);
        debugRequestId = id;
        debugCancellation = cancellation;
        return (id, cancellation);
    }

    private void EndDebug(CancellationTokenSource cancellation)
    {
        if (debugCancellation == cancellation)
        {
            debugCancellation = null;
            debugRequestId = null;
        }
        cancellation.Dispose();
    }

    private void DebugPage()
    {
        var pivot = new Pivot();
        var providers = vm.State["providers"]!.AsArray();
        var allModels = vm.State["models"]!.AsArray().Where(m => m!["enabled"]?.GetValue<bool>() == true &&
            providers.Any(p => p?["enabled"]?.GetValue<bool>() == true && p["id"]?.ToString() == m["provider_id"]?.ToString())).ToArray();
        var selectedModel = Combo(Locale.Text("Model profile"), allModels.Select(m => new Choice(m!["id"]!.ToString(), m["name"]!.ToString())), debugPreset ?? vm.MainModelId);
        var kind = Combo(Locale.Text("Test"), Array.Empty<Choice>());
        var details = Text("", 12);
        var prompt = Field(Locale.Text("Input text"), Locale.Text("Hello, Jui. Good morning. Let's get started."), true);
        prompt.MinHeight = 88;
        var result = Field(Locale.Text("Output"), "", true); result.IsReadOnly = true; result.MinHeight = 200;
        var status = Text(Locale.Text("Ready"), 12);
        var voice = Field(Locale.Text("Voice ID for this test"));
        var speed = Number(Locale.Text("Speed for this test"), 1, 0.25, 4);
        var language = Combo(Locale.Text("Preview language"), new[] { new Choice("en_us", Locale.Text("American English")), new Choice("en_gb", Locale.Text("British English")), new Choice("cmn", Locale.Text("Mandarin Chinese")) });
        var voiceChoices = Combo(Locale.Text("Available voices"), Array.Empty<Choice>()); voiceChoices.Visibility = Visibility.Collapsed;
        var selectedFile = Text(Locale.Text("No file selected"), 12);
        string? image = null;
        byte[]? recording = null;
        string imageName = "", recordingName = "";
        SpeechSample? baseline = null;
        SpeechSample? latest = null;
        bool busy = false;
        var comparison = Text("", 12);
        var useBaseline = Icon(Symbol.Back, Locale.Text("Use A's text"), () => { if (!busy && baseline != null) prompt.Text = baseline.Text; return Task.CompletedTask; });
        Task Play(SpeechSample? sample) { if (sample != null) { StopSpeech(); audio.Play(sample.Wav, outputDevice); } return Task.CompletedTask; }
        var playA = Command(Locale.Text("Play A"), Symbol.Volume, () => Play(baseline));
        var playB = Command(Locale.Text("Play B"), Symbol.Volume, () => Play(latest));
        void UpdateComparison()
        {
            playA.IsEnabled = useBaseline.IsEnabled = baseline != null;
            playB.IsEnabled = latest != null;
            string Describe(string slot, SpeechSample? sample) => sample == null ? Locale.Format("{0} · No sample", slot) : Locale.Format("{0} · {1} · {2} · {3} · Generated in {4:F2} s", slot, sample.Model, sample.Voice, sample.Language, sample.Seconds);
            comparison.Text = $"{Describe("A", baseline)}\n{Describe("B", latest)}";
            if (baseline != null && latest != null)
                comparison.Text += baseline.Text == latest.Text ? Locale.Text("\nMatching text") : Locale.Text("\nDifferent text");
        }
        var pin = Icon(Symbol.Pin, Locale.Text("Pin B as A"), () => { if (latest != null) baseline = latest; UpdateComparison(); return Task.CompletedTask; });
        var clearA = Icon(Symbol.Clear, Locale.Text("Clear A"), () => { baseline = null; UpdateComparison(); return Task.CompletedTask; });
        var comparisonPanel = Stack(Row(playA, playB, pin, useBaseline, clearA), comparison);
        UpdateComparison();
        var copy = Icon(Symbol.Copy, Locale.Text("Copy result"), () => { var data = new DataPackage(); data.SetText(result.Text); Clipboard.SetContent(data); return Task.CompletedTask; });
        JsonNode? Profile() => allModels.FirstOrDefault(m => m!["id"]!.ToString() == Selected(selectedModel));
        JsonNode? Provider() => vm.State["providers"]!.AsArray().FirstOrDefault(p => p!["id"]!.ToString() == Profile()?["provider_id"]?.ToString());
        var loadVoices = Command(Locale.Text("Load voices"), Symbol.Refresh, async () =>
        {
            var id = Selected(selectedModel);
            var revision = debugRevision;
            status.Text = Locale.Text("Loading voices");
            try
            {
                var values = await vm.Client.Get($"api/v1/debug/models/{id}/voices");
                if (revision != debugRevision || id != Selected(selectedModel) || Selected(kind) != "tts") return;
                voiceChoices.Items.Clear();
                foreach (var item in values["voices"]!.AsArray()) voiceChoices.Items.Add(new Choice(item!["id"]!.ToString(), item["name"]!.ToString()));
                voiceChoices.SelectedItem = voiceChoices.Items.Cast<Choice>().FirstOrDefault(c => c.Id == voice.Text) ?? voiceChoices.Items.Cast<Choice>().FirstOrDefault();
                voiceChoices.Visibility = Visibility.Visible; status.Text = Locale.Text("Voices loaded");
            }
            catch { status.Text = Locale.Text("Could not load voices"); throw; }
        });
        voiceChoices.SelectionChanged += (_, _) => { if (voiceChoices.SelectedItem is Choice choice) voice.Text = choice.Id; };
        var speechOptions = new Expander { Header = Locale.Text("Voice and speed"), Content = Stack(voice, loadVoices, voiceChoices, speed), HorizontalAlignment = HorizontalAlignment.Stretch, HorizontalContentAlignment = HorizontalAlignment.Stretch };
        var pick = Command(Locale.Text("Choose file"), Symbol.OpenFile, async () =>
        {
            var revision = debugRevision;
            var selection = Selected(selectedModel);
            var mode = Selected(kind);
            var picker = new FileOpenPicker();
            foreach (var extension in mode == "vision" ? new[] { ".png", ".jpg", ".jpeg" } : new[] { ".wav", ".mp3", ".flac", ".m4a" }) picker.FileTypeFilter.Add(extension);
            InitializeWithWindow.Initialize(picker, WindowNative.GetWindowHandle(this));
            var file = await picker.PickSingleFileAsync(); if (file == null) return;
            bool Current() => revision == debugRevision && selection == Selected(selectedModel) && mode == Selected(kind) && !busy;
            if (!Current()) return;
            if (mode == "vision")
            {
                if (new FileInfo(file.Path).Length > 2_900_000) throw new InvalidOperationException(Locale.Text("Images must be 2.9 MB or smaller."));
                var bytes = await File.ReadAllBytesAsync(file.Path);
                if (bytes.Length > 2_900_000) throw new InvalidOperationException(Locale.Text("Images must be 2.9 MB or smaller."));
                if (!Current()) return;
                image = $"data:image/{(file.FileType.ToLowerInvariant() == ".png" ? "png" : "jpeg")};base64," + Convert.ToBase64String(bytes);
                imageName = file.Name;
            }
            else
            {
                var wav = await Task.Run(() => AudioService.LoadRecording(file.Path));
                if (!Current()) return;
                recording = wav;
                recordingName = file.Name;
            }
            selectedFile.Text = file.Name;
        });
        Button? run = null;
        Button? record = null;

        async Task<bool> ExternalConsent()
        {
            if (Provider()?["local"]?.GetValue<bool>() == true) return true;
            return await Confirm(Locale.Text("Test external model"), Locale.Text("The selected API will receive your input, audio, or image and may charge for this request. Your workspace sharing permissions still apply."));
        }

        void SetBusy(bool value)
        {
            busy = value; selectedModel.IsEnabled = kind.IsEnabled = pick.IsEnabled = loadVoices.IsEnabled = !value;
            loadVoices.IsEnabled = !value && Provider()?["protocol"]?.ToString() != "native-tts";
            prompt.IsEnabled = voiceChoices.IsEnabled = language.IsEnabled = !value;
            run!.IsEnabled = !value; record!.IsEnabled = !value || debugRecording;
        }

        async Task Execute()
        {
            if (busy || Profile() == null) return;
            if (debugCancellation != null) throw new InvalidOperationException(Locale.Text("Stop the active test first."));
            if (Selected(kind) == "asr" && recording == null) throw new InvalidOperationException(Locale.Text("Record or select audio first."));
            if (Selected(kind) == "vision" && image == null) throw new InvalidOperationException(Locale.Text("Select an image first."));
            var model = Selected(selectedModel); var mode = Selected(kind);
            var revision = debugRevision;
            var text = prompt.Text;
            if (mode != "asr" && string.IsNullOrWhiteSpace(text)) throw new InvalidOperationException(Locale.Text("Enter some test text."));
            SetBusy(true);
            CancellationTokenSource? cancellation = null;
            try
            {
                var provider = Provider()!;
                if (!await ExternalConsent() || revision != debugRevision) return;
                if (debugCancellation != null) throw new InvalidOperationException(Locale.Text("Stop the active test first."));
                StopSpeech();
                var request = BeginDebug(); cancellation = request.Cancellation;
                var token = cancellation.Token;
                var identity = request.Id;
                var stopwatch = Stopwatch.StartNew(); status.Text = Locale.Text("Waiting for the model"); result.Text = "";
                if (mode == "tts")
                {
                    var native = provider["protocol"]?.ToString() == "native-tts";
                    var testLanguage = Profile()?["provider_id"]?.ToString() == "zonos3" ? Selected(language) : null;
                    var voiceId = native ? null : voice.Text;
                    var voiceName = (voiceChoices.SelectedItem is Choice choice && choice.Id == voice.Text) ? choice.Name : (voice.Text == Profile()?["voice"]?.ToString() || native ? Profile()?["name"]?.ToString()?.Split(" / ").Last() ?? voice.Text : voice.Text);
                    var languageName = testLanguage == null ? Locale.Text("Profile default") : ((Choice)language.SelectedItem).Name;
                    var wav = await vm.Client.DebugSpeech(new { request_id = identity, model_ref = model, workspace_id = vm.WorkspaceId, confirm_external = true, text, voice = voiceId, speed = speed.IsEnabled ? (double?)speed.Value : null, language = testLanguage }, token);
                    token.ThrowIfCancellationRequested();
                    var (duration, rate) = AudioService.DescribeWav(wav);
                    latest = new SpeechSample(wav, Profile()!["name"]!.ToString(), text, voiceName, languageName, stopwatch.Elapsed.TotalSeconds);
                    UpdateComparison();
                    result.Text = Locale.Format("{0}\n{1:F2} s of audio · {2:g} kHz\n\n{3}", latest.Model, duration.TotalSeconds, rate / 1000.0, text);
                    audio.Play(wav, outputDevice);
                }
                else if (mode == "asr")
                {
                    var value = await vm.Client.DebugTranscribe(recording!, model, vm.WorkspaceId, identity, true, token);
                    result.Text = value["text"]?.ToString() ?? "";
                }
                else
                {
                    var value = await vm.Client.DebugText(new { request_id = identity, model_ref = model, workspace_id = vm.WorkspaceId, confirm_external = true, prompt = text, mode, image = mode == "vision" ? image : null }, token);
                    result.Text = value["text"]?.ToString() ?? "";
                    if (mode == "tools") result.Text += Locale.Text("\n\nProposed tool calls (not executed)\n") + value["tool_calls"]?.ToJsonString(new() { WriteIndented = true });
                }
                status.Text = Locale.Format("Completed · {0:F2} s", stopwatch.Elapsed.TotalSeconds);
            }
            catch (OperationCanceledException) { status.Text = Locale.Text("Canceled"); }
            catch (Exception error) { status.Text = Locale.Text("Test failed"); result.Text = error.Message; }
            finally { if (cancellation != null) EndDebug(cancellation); SetBusy(false); }
        }

        async Task TranscribeRecording()
        {
            var revision = debugRevision;
            try
            {
                var wav = await FinishDebugRecording();
                if (revision != debugRevision) return;
                recording = wav; recordingName = selectedFile.Text = Locale.Text("Current recording");
                SetBusy(false); await Execute();
            }
            finally { if (revision == debugRevision) SetBusy(false); }
        }

        record = Command(Locale.Text("Record / Stop"), Symbol.Microphone, async () =>
        {
            if (debugRecording) { await TranscribeRecording(); return; }
            if (debugCancellation != null) throw new InvalidOperationException(Locale.Text("Stop the active test first."));
            if (audio.Recording) throw new InvalidOperationException(Locale.Text("Stop recording on the other page first."));
            StopSpeech(); audio.Start(inputDevice); debugRecording = true; SetBusy(true); status.Text = Locale.Text("Recording");
            debugRecordTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(39) };
            debugRecordTimer.Tick += (_, _) => { debugRecordTimer.Stop(); if (debugRecording) Run(TranscribeRecording); };
            debugRecordTimer.Start();
        });
        run = Command(Locale.Text("Run test"), Symbol.Play, Execute);
        var cancel = Icon(Symbol.Stop, Locale.Text("Cancel test / Stop playback"), async () => { var wasRecording = debugRecording || debugRecordingStop != null; await CancelDebug(); status.Text = Locale.Text("Stopped"); if (wasRecording) SetBusy(false); });
        var fileOptions = Stack(Row(pick, record), selectedFile);
        var examples = Combo(Locale.Text("Preview text"), new[] { new Choice("", Locale.Text("Choose a sample")), new("Hello, Jui.", "Hello, Jui."), new("Good morning. How are you?", "Good morning. How are you?"), new("Let's get started.", "Let's get started."), new(Locale.Text("Hello, how is your day going?"), Locale.Text("Hello, how is your day going?")) });
        examples.SelectionChanged += (_, _) =>
        {
            if (busy || string.IsNullOrEmpty(Selected(examples))) return;
            prompt.Text = Selected(examples);
            examples.SelectedIndex = 0;
        };
        bool adapting = false;
        string previousMode = "";
        var drafts = new Dictionary<string, string>
        {
            ["tts"] = Locale.Text("Hello, Jui. Good morning. Let's get started."),
            ["text"] = Locale.Text("Reply with a short greeting in English."),
            ["tools"] = Locale.Text("Call the debug_echo tool once with the text Hello, Jui."),
            ["vision"] = Locale.Text("Describe this image briefly in English.")
        };
        void AdaptMode()
        {
            var mode = Selected(kind);
            if (drafts.ContainsKey(previousMode)) drafts[previousMode] = prompt.Text;
            if (drafts.TryGetValue(mode, out var draft)) prompt.Text = draft;
            previousMode = mode;
            prompt.Visibility = mode == "asr" ? Visibility.Collapsed : Visibility.Visible;
            examples.Visibility = speechOptions.Visibility = mode == "tts" ? Visibility.Visible : Visibility.Collapsed;
            comparisonPanel.Visibility = mode == "tts" ? Visibility.Visible : Visibility.Collapsed;
            language.Visibility = mode == "tts" && Profile()?["provider_id"]?.ToString() == "zonos3" ? Visibility.Visible : Visibility.Collapsed;
            fileOptions.Visibility = mode is "asr" or "vision" ? Visibility.Visible : Visibility.Collapsed;
            record!.Visibility = mode == "asr" ? Visibility.Visible : Visibility.Collapsed;
            prompt.Header = mode == "tools" ? Locale.Text("Tool-call test prompt") : Locale.Text("Input text");
            selectedFile.Text = mode == "vision" && image != null ? imageName : mode == "asr" && recording != null ? recordingName : Locale.Text("No file selected");
            result.Text = "";
            status.Text = Locale.Text("Ready");
            run!.IsEnabled = kind.Items.Count != 0;
        }
        void AdaptModel()
        {
            ++debugRevision;
            adapting = true; kind.Items.Clear();
            var profile = Profile();
            foreach (var (id, label) in new[] { ("text", Locale.Text("Text generation")), ("tools", Locale.Text("Propose a tool call")), ("vision", Locale.Text("Image understanding")), ("tts", Locale.Text("Text to speech")), ("asr", Locale.Text("Speech to text")) })
                if (profile?["capabilities"]?.AsArray().Any(c => c?.ToString() == id) == true) kind.Items.Add(new Choice(id, label));
            kind.SelectedIndex = kind.Items.Count > 0 ? 0 : -1;
            voice.Text = profile?["voice"]?.ToString() ?? "alloy"; speed.Value = profile?["speed"]?.GetValue<double>() ?? 1;
            voice.IsEnabled = loadVoices.IsEnabled = Provider()?["protocol"]?.ToString() != "native-tts";
            if (!voice.IsEnabled) voice.Text = profile?["name"]?.ToString() ?? Locale.Text("Built-in voice");
            ToolTipService.SetToolTip(voice, voice.IsEnabled ? Locale.Text("For this test only") : Locale.Text("This local adapter uses its saved reference voice"));
            speed.IsEnabled = profile?["provider_id"]?.ToString() != "zonos3" && Provider()?["protocol"]?.ToString() != "native-tts";
            speed.Visibility = speed.IsEnabled ? Visibility.Visible : Visibility.Collapsed;
            ToolTipService.SetToolTip(speed, speed.IsEnabled ? Locale.Text("For this test only") : Locale.Text("This endpoint has no standard speed parameter"));
            if (previousMode != "tts" || Selected(kind) != "tts" || language.Visibility != Visibility.Visible)
                language.SelectedItem = language.Items.Cast<Choice>().FirstOrDefault(c => c.Id == profile?["language"]?.ToString()) ?? language.Items[0];
            voiceChoices.Items.Clear(); voiceChoices.Visibility = Visibility.Collapsed;
            details.Text = profile == null ? Locale.Text("Add a connection and enable a model profile in Models and connections.") : $"{Provider()?["name"]} · {Provider()?["base_url"]}";
            debugPreset = Selected(selectedModel); adapting = false; AdaptMode();
        }
        selectedModel.SelectionChanged += (_, _) => AdaptModel();
        kind.SelectionChanged += (_, _) => { if (!adapting) { ++debugRevision; AdaptMode(); } };
        AdaptModel();
        debugSpeechSmoke = async text =>
        {
            kind.SelectedItem = kind.Items.Cast<Choice>().First(c => c.Id == "tts"); prompt.Text = text; await Execute();
            return (new JsonObject { ["model"] = Selected(selectedModel), ["status"] = status.Text, ["output"] = result.Text, ["bytes"] = latest?.Wav.Length ?? 0 }, latest?.Wav);
        };
        debugAsrSmoke = async wav =>
        {
            kind.SelectedItem = kind.Items.Cast<Choice>().First(c => c.Id == "asr"); recording = AudioService.NormalizeWav(wav); recordingName = selectedFile.Text = Locale.Text("Synthesized audio"); await Execute();
            return new JsonObject { ["model"] = Selected(selectedModel), ["status"] = status.Text, ["output"] = result.Text };
        };
        debugTextSmoke = async (mode, text) =>
        {
            kind.SelectedItem = kind.Items.Cast<Choice>().First(c => c.Id == mode); prompt.Text = text; await Execute();
            return new JsonObject { ["model"] = Selected(selectedModel), ["status"] = status.Text, ["output"] = result.Text };
        };
        var selectors = new Grid { ColumnSpacing = 12 };
        selectors.ColumnDefinitions.Add(new ColumnDefinition()); selectors.ColumnDefinitions.Add(new ColumnDefinition());
        selectors.Children.Add(selectedModel); Grid.SetColumn(kind, 1); selectors.Children.Add(kind);
        var body = new Grid { ColumnSpacing = 20, RowSpacing = 16 };
        body.ColumnDefinitions.Add(new ColumnDefinition()); body.ColumnDefinitions.Add(new ColumnDefinition());
        body.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto }); body.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        debugCompareSmoke = async () =>
        {
            baseline = latest; UpdateComparison();
            var alternate = selectedModel.Items.Cast<Choice>().FirstOrDefault(c => c.Id != Selected(selectedModel) && LocalDebugProfiles("tts").Contains(c.Id));
            if (baseline == null || alternate == null) return new JsonObject { ["model"] = "a-b", ["status"] = Locale.Text("Incomplete") };
            selectedModel.SelectedItem = alternate;
            kind.SelectedItem = kind.Items.Cast<Choice>().First(c => c.Id == "tts");
            await Execute();
            var matched = latest != null && baseline.Model != latest.Model && baseline.Text == latest.Text &&
                playA.IsEnabled && playB.IsEnabled && comparison.Text.Contains(Locale.Text("Matching text"));
            return new JsonObject { ["model"] = "a-b", ["status"] = matched ? Locale.Text("Completed") : Locale.Text("Incomplete"), ["output"] = comparison.Text };
        };
        var inputs = Stack(prompt, fileOptions, Row(run, cancel, copy), status, comparisonPanel, examples, language, speechOptions);
        body.Children.Add(inputs); Grid.SetColumn(result, 1); body.Children.Add(result); result.MinHeight = 350; result.MaxHeight = 650;
        body.SizeChanged += (_, _) => { var narrow = body.ActualWidth < 650; body.ColumnDefinitions[1].Width = narrow ? new GridLength(0) : new GridLength(1, GridUnitType.Star); Grid.SetColumn(result, narrow ? 0 : 1); Grid.SetRow(result, narrow ? 1 : 0); };
        var panel = Stack(selectors, details, body);
        panel.MaxWidth = 1000; panel.HorizontalAlignment = HorizontalAlignment.Stretch;
        pivot.Items.Add(new PivotItem { Header = Locale.Text("Model testing"), Content = new ScrollViewer { Content = panel, HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled } });
        pivot.Items.Add(new PivotItem { Header = Locale.Text("Search connection"), Content = DebugSearchPanel() });
        pageHost.Children.Add(pivot);
    }

    private ScrollViewer DebugSearchPanel()
    {
        var providers = vm.State["providers"]!.AsArray().Where(p => p?["enabled"]?.GetValue<bool>() == true && (p["protocol"]?.ToString() is "brave" or "tavily" or "searxng"));
        var connection = Combo(Locale.Text("Search connection"), providers.Select(p => new Choice(p!["id"]!.ToString(), p["name"]!.ToString())));
        var query = Field(Locale.Text("Test query"), "latest science news");
        var language = Field(Locale.Text("Language"), "en"); var region = Field(Locale.Text("Region"), "us"); var count = Number(Locale.Text("Result count"), 5, 1, 10);
        var result = Field(Locale.Text("Search results"), "", true); result.IsReadOnly = true;
        var status = Text(Locale.Text("Ready"), 12);
        void Adapt()
        {
            var protocol = providers.FirstOrDefault(p => p!["id"]!.ToString() == Selected(connection))?["protocol"]?.ToString();
            language.IsEnabled = protocol is "brave" or "searxng"; region.IsEnabled = protocol == "brave";
            ToolTipService.SetToolTip(language, language.IsEnabled ? Locale.Text("Language for this query") : Locale.Text("This provider does not support this parameter"));
            ToolTipService.SetToolTip(region, region.IsEnabled ? Locale.Text("Region for this query") : Locale.Text("This provider does not support this parameter"));
        }
        connection.SelectionChanged += (_, _) => Adapt(); Adapt();
        var run = Command(Locale.Text("Test search connection"), Symbol.Find, async () =>
        {
            if (string.IsNullOrEmpty(Selected(connection))) throw new InvalidOperationException(Locale.Text("Add a search connection first."));
            if (debugCancellation != null || debugRecording) throw new InvalidOperationException(Locale.Text("Stop the active test first."));
            var revision = debugRevision;
            var providerId = Selected(connection);
            var provider = providers.First(p => p!["id"]!.ToString() == providerId)!;
            if (provider["local"]?.GetValue<bool>() != true && !await Confirm(Locale.Text("Test web search"), Locale.Text("Only this query is sent to the selected search service. API fees may apply."))) return;
            if (revision != debugRevision || debugCancellation != null || debugRecording) return;
            var request = BeginDebug();
            status.Text = Locale.Text("Waiting for search results");
            result.Text = "";
            try
            {
                var value = await vm.Client.DebugSearch(new { request_id = request.Id, provider_ref = providerId, query = query.Text, language = language.Text, region = region.Text, count = (int)count.Value, confirm_external = true }, request.Cancellation.Token);
                result.Text = value["results"]!.ToJsonString(new() { WriteIndented = true }); status.Text = Locale.Format("Completed · {0} ms", value["elapsed_ms"]);
            }
            catch (OperationCanceledException) { status.Text = Locale.Text("Canceled"); }
            catch (Exception error) { status.Text = Locale.Text("Test failed"); result.Text = error.Message; }
            finally { EndDebug(request.Cancellation); }
        });
        return new ScrollViewer { Content = Stack(connection, query, language, region, count, Row(run, Icon(Symbol.Stop, Locale.Text("Cancel search test"), CancelDebug)), status, result), HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled };
    }
}
