using System.Diagnostics;
using System.Text.Json.Nodes;
using JuiAI.ControlCenter.ViewModels;
using Microsoft.UI.Text;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Windows.Storage;
using Windows.Storage.Pickers;
using Windows.System;
using WinRT.Interop;

namespace JuiAI.ControlCenter;

public sealed partial class MainWindow
{
    private const string VoiceProcessingApi = "api/v1/voice-processing";
    private string? voiceProcessingInput;
    private string? voiceProcessingFolder;
    private string voiceProcessingMode = "inworld";
    private string voiceProcessingDevice = "auto";
    private bool voiceProcessingBackground = true;
    private bool voiceProcessingSubmitting;
    private bool voiceProcessingCancelling;
    private JsonNode? voiceProcessingJob;
    private Action? renderVoiceProcessing;

    private async Task VoiceProcessingPage()
    {
        voiceProcessingFolder ??= Path.Combine(vm.Client.Root, "14_Workspace");
        JsonNode? availability = null;
        var runtime = Text("Checking local models…", 13);
        var input = VoicePathField("Input audio", "Choose a local audio file");
        var folder = VoicePathField("Output location", "Choose an output folder");
        var mode = Combo("Processing mode", [new("inworld", "Inworld voice isolation"), new("uvr_inworld", "UVR separation + Inworld cleanup")], voiceProcessingMode);
        var device = Combo("Inworld inference device", [new("auto", "Automatic"), new("cuda", "CUDA"), new("cpu", "CPU")], voiceProcessingDevice);
        var modeDetails = Text("", 13);
        var background = new CheckBox { Content = "Save audio removed by Inworld", IsChecked = voiceProcessingBackground };
        var start = new Button { Content = "Start processing", Style = (Style)Application.Current.Resources["AccentButtonStyle"] };
        var cancel = new Button { Content = "Cancel task" };
        var status = Text("", 13);
        var progress = new ProgressBar { Visibility = Visibility.Collapsed, Minimum = 0, Maximum = 100 };
        var result = Stack(Text("Processing results", 18));
        var resultBody = Stack();
        result.Children.Add(resultBody);
        JsonNode? shownResult = null;

        var chooseInput = Command("Choose audio", Symbol.OpenFile, async () =>
        {
            var picker = new FileOpenPicker { SuggestedStartLocation = PickerLocationId.MusicLibrary };
            foreach (var suffix in new[] { ".wav", ".flac", ".mp3", ".ogg", ".opus", ".m4a", ".aac", ".wma", ".aif", ".aiff", ".webm" })
                picker.FileTypeFilter.Add(suffix);
            InitializeWithWindow.Initialize(picker, WindowNative.GetWindowHandle(this));
            if (await picker.PickSingleFileAsync() is { } file) voiceProcessingInput = file.Path;
            renderVoiceProcessing?.Invoke();
        });
        var chooseFolder = Command("Choose folder", Symbol.Folder, async () =>
        {
            voiceProcessingFolder = await PickFolder() ?? voiceProcessingFolder;
            renderVoiceProcessing?.Invoke();
        });
        var openUvr = Command("Open UVR", Symbol.OpenFile, () =>
        {
            var root = availability?["uvr_root"]?.ToString();
            if (string.IsNullOrWhiteSpace(root)) throw new InvalidOperationException("Set the Ultimate Vocal Remover installation folder first.");
            var executable = Path.Combine(root, "UVR.exe");
            if (!File.Exists(executable)) throw new InvalidOperationException("Could not find UVR.exe in the Ultimate Vocal Remover installation.");
            using var process = Process.Start(new ProcessStartInfo(executable) { WorkingDirectory = root, UseShellExecute = true });
            return Task.CompletedTask;
        });
        openUvr.IsEnabled = false;
        openUvr.HorizontalAlignment = HorizontalAlignment.Right;

        var title = Text("Inworld Voice Isolator", 26);
        title.FontWeight = FontWeights.SemiBold;
        var intro = VoiceProcessingCard(VoiceProcessingColumns(
            Stack(title, Text("Isolate speech from a recording on this computer. Optionally separate vocals and accompaniment first with Ultimate Vocal Remover."), runtime), openUvr, GridLength.Auto));
        var form = Stack(Text("Processing settings", 18), VoiceProcessingColumns(input, chooseInput, GridLength.Auto),
            VoiceProcessingColumns(folder, chooseFolder, GridLength.Auto),
            VoiceProcessingColumns(mode, device, new GridLength(0.78, GridUnitType.Star)), modeDetails, background,
            status, progress, Row(start, cancel));
        form.Spacing = 9;
        var resultCard = VoiceProcessingCard(result);
        var columns = VoiceProcessingColumns(VoiceProcessingCard(form), resultCard, new GridLength(0.8, GridUnitType.Star));
        columns.SizeChanged += (_, args) =>
        {
            var singleColumn = args.NewSize.Width < 760;
            columns.ColumnDefinitions[1].Width = singleColumn ? new GridLength(0) : new GridLength(0.8, GridUnitType.Star);
            Grid.SetColumn(resultCard, singleColumn ? 0 : 1);
            Grid.SetRow(resultCard, singleColumn ? 1 : 0);
        };
        columns.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        columns.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        columns.RowSpacing = 12;
        var content = Stack(intro, columns);
        content.MaxWidth = 1140;
        Scrolled(content);

        void Render()
        {
            var job = voiceProcessingJob;
            var jobStatus = job?["status"]?.ToString();
            var stage = job?["stage"]?.ToString();
            var active = IsVoiceProcessingActive(job);
            var busy = active || voiceProcessingSubmitting;
            var useUvr = voiceProcessingMode == "uvr_inworld";
            input.Text = voiceProcessingInput ?? "";
            folder.Text = voiceProcessingFolder ?? "";
            ToolTipService.SetToolTip(input, voiceProcessingInput);
            ToolTipService.SetToolTip(folder, voiceProcessingFolder);
            chooseInput.IsEnabled = chooseFolder.IsEnabled = mode.IsEnabled = device.IsEnabled = background.IsEnabled = !busy;
            start.IsEnabled = !busy && voiceProcessingInput != null && availability?["inworld_ready"]?.GetValue<bool>() == true
                && (!useUvr || availability["uvr_ready"]?.GetValue<bool>() == true);
            cancel.Visibility = busy ? Visibility.Visible : Visibility.Collapsed;
            cancel.IsEnabled = active && jobStatus != "cancelling" && !voiceProcessingCancelling;
            modeDetails.Text = useUvr
                ? availability?["uvr_ready"]?.GetValue<bool>() == true
                    ? "UVR separates vocals and accompaniment before Inworld cleans the vocal track. All separated tracks are saved."
                    : "Combined processing requires an available UVR separation model and runtime."
                : "For dialogue, narration, and live recordings. Output is 48 kHz mono WAV.";
            status.Text = voiceProcessingSubmitting ? "Submitting task…" : jobStatus switch
            {
                "queued" => "Waiting for local inference resources…",
                "running" when stage == "waiting" => "Waiting for local inference resources…",
                "running" when stage == "uvr" => "UVR is separating vocals and accompaniment…",
                "running" => "Inworld is isolating speech…",
                "cancelling" => "Canceling task…",
                "cancelled" => "Task canceled.",
                "failed" => job?["error"]?.ToString() ?? "Processing failed.",
                "completed" => "Processing complete.",
                _ => "Each run creates a separate output folder in the selected location."
            };
            var percent = job?["progress"]?.GetValue<double>();
            progress.Visibility = busy ? Visibility.Visible : Visibility.Collapsed;
            progress.IsIndeterminate = percent is null;
            progress.Value = percent ?? 0;

            var completed = !voiceProcessingSubmitting && jobStatus == "completed" ? job : null;
            if (resultBody.Children.Count == 0 || completed != shownResult)
            {
                shownResult = completed;
                ShowVoiceProcessingResult(resultBody, completed);
            }
        }

        mode.SelectionChanged += (_, _) => { voiceProcessingMode = Selected(mode); Render(); };
        device.SelectionChanged += (_, _) => voiceProcessingDevice = Selected(device);
        background.Checked += (_, _) => voiceProcessingBackground = true;
        background.Unchecked += (_, _) => voiceProcessingBackground = false;
        start.Click += (_, _) => Run(StartVoiceProcessing);
        cancel.Click += (_, _) => Run(CancelVoiceProcessing);
        renderVoiceProcessing = Render;
        Render();
        try
        {
            availability = await vm.Client.Get($"{VoiceProcessingApi}/status", 8);
            openUvr.IsEnabled = !string.IsNullOrWhiteSpace(availability["uvr_root"]?.ToString()) && File.Exists(Path.Combine(availability["uvr_root"]!.ToString(), "UVR.exe"));
            var inworldReady = availability["inworld_ready"]?.GetValue<bool>() == true;
            var uvrReady = availability["uvr_ready"]?.GetValue<bool>() == true;
            runtime.Text = $"Inworld · {(inworldReady ? "Available locally" : "Runtime unavailable")}    UVR separation · {(uvrReady ? "Available locally" : "Unavailable")}";
        }
        catch (Exception error) when (error is HttpRequestException or InvalidOperationException or TaskCanceledException)
        {
            runtime.Text = "Could not read local model status. Select Refresh in the upper right to try again.";
        }
        Render();
        if (IsVoiceProcessingActive(voiceProcessingJob) && voiceProcessingJob?["id"]?.ToString() is { } id)
            await ReadVoiceProcessingJob(id);
    }

    private async Task StartVoiceProcessing()
    {
        if (voiceProcessingSubmitting || IsVoiceProcessingActive(voiceProcessingJob) || voiceProcessingInput == null || voiceProcessingFolder == null) return;
        StopSpeech();
        voiceProcessingSubmitting = true;
        renderVoiceProcessing?.Invoke();
        try
        {
            voiceProcessingJob = await vm.Client.Post($"{VoiceProcessingApi}/jobs", new
            {
                input_path = voiceProcessingInput, output_dir = voiceProcessingFolder, mode = voiceProcessingMode,
                device = voiceProcessingDevice, save_background = voiceProcessingBackground
            }, 15);
        }
        finally
        {
            voiceProcessingSubmitting = false;
            renderVoiceProcessing?.Invoke();
        }
        if (IsVoiceProcessingActive(voiceProcessingJob) && voiceProcessingJob["id"]?.ToString() is { } id)
            await ReadVoiceProcessingJob(id);
    }

    private async Task CancelVoiceProcessing()
    {
        if (voiceProcessingCancelling || !IsVoiceProcessingActive(voiceProcessingJob) || voiceProcessingJob?["id"]?.ToString() is not { } id) return;
        voiceProcessingCancelling = true;
        renderVoiceProcessing?.Invoke();
        try { ApplyVoiceProcessingJob(id, await vm.Client.Post($"{VoiceProcessingApi}/jobs/{id}/cancel", seconds: 10)); }
        finally { voiceProcessingCancelling = false; renderVoiceProcessing?.Invoke(); }
    }

    private static bool IsVoiceProcessingActive(JsonNode? job) => job?["status"]?.ToString() is "queued" or "running" or "cancelling";
    private async Task OnVoiceProcessingEvent(JsonNode data)
    {
        if (data["id"]?.ToString() is { } id && id == voiceProcessingJob?["id"]?.ToString()
            && (data["revision"]?.GetValue<long>() ?? 0) > (voiceProcessingJob["revision"]?.GetValue<long>() ?? 0))
            await ReadVoiceProcessingJob(id);
    }
    private async Task ReadVoiceProcessingJob(string id) => ApplyVoiceProcessingJob(id, await vm.Client.Get($"{VoiceProcessingApi}/jobs/{id}", 10));
    private void ApplyVoiceProcessingJob(string id, JsonNode job)
    {
        if (voiceProcessingJob?["id"]?.ToString() != id || job["id"]?.ToString() != id
            || (job["revision"]?.GetValue<long>() ?? 0) <= (voiceProcessingJob["revision"]?.GetValue<long>() ?? 0)) return;
        voiceProcessingJob = job;
        renderVoiceProcessing?.Invoke();
    }

    private void ShowVoiceProcessingResult(StackPanel panel, JsonNode? job)
    {
        panel.Children.Clear();
        if (job == null)
        {
            panel.Children.Add(Text("When processing finishes, preview each track and open the output folder in File Explorer.", 13));
            return;
        }
        var sampleRate = job["sample_rate"]?.GetValue<int>() ?? 48000;
        var seconds = (job["sample_count"]?.GetValue<long>() ?? 0) / (double)sampleRate;
        var devices = "Inworld · " + (job["device"]?.ToString() ?? "").ToUpperInvariant();
        if (job["uvr_device"]?.ToString() is { } uvr) devices += "    UVR · " + uvr.ToUpperInvariant();
        panel.Children.Add(Text($"{devices}\n{sampleRate / 1000.0:g} kHz · {seconds:0.0} s", 13));
        if (job["output_dir"]?.ToString() is { } directory)
        {
            panel.Children.Add(Row(Command("Open output folder", Symbol.Folder, async () =>
            {
                if (!await Launcher.LaunchFolderAsync(await StorageFolder.GetFolderFromPathAsync(directory)))
                    throw new InvalidOperationException("Could not open the output folder.");
            }), Icon(Symbol.Stop, "Stop preview", () => { audio.StopPlayback(); return Task.CompletedTask; })));
        }
        foreach (var (key, label) in new[] { ("voice_path", "Clean vocals"), ("background_path", "Audio removed by Inworld"), ("vocals_path", "Original UVR vocals"), ("instrumental_path", "UVR accompaniment") })
        {
            if (job[key]?.ToString() is not { } path) continue;
            var track = Stack(Text(label), Text(Path.GetFileName(path), 12));
            track.Spacing = 4;
            var row = ItemRow(track, Icon(Symbol.Play, $"Preview{label}", () =>
            {
                StopSpeech();
                audio.PlayFile(path, outputDevice);
                return Task.CompletedTask;
            }));
            row.Padding = new Thickness(0, 6, 0, 6);
            panel.Children.Add(row);
        }
    }

    private static TextBox VoicePathField(string header, string placeholder) => new()
    {
        Header = header, IsReadOnly = true, PlaceholderText = placeholder, TextWrapping = TextWrapping.NoWrap
    };

    private static Border VoiceProcessingCard(FrameworkElement content) => new()
    {
        Background = (Brush)Application.Current.Resources["CardBackgroundFillColorDefaultBrush"],
        BorderBrush = (Brush)Application.Current.Resources["CardStrokeColorDefaultBrush"],
        BorderThickness = new Thickness(1), CornerRadius = new CornerRadius(8), Padding = new Thickness(18),
        VerticalAlignment = VerticalAlignment.Top, Child = content
    };

    private static Grid VoiceProcessingColumns(FrameworkElement left, FrameworkElement right, GridLength rightWidth)
    {
        var grid = new Grid { ColumnSpacing = 12 };
        grid.ColumnDefinitions.Add(new ColumnDefinition());
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = rightWidth });
        grid.Children.Add(left);
        Grid.SetColumn(right, 1);
        grid.Children.Add(right);
        return grid;
    }
}
