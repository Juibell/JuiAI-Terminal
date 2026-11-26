using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Media.Imaging;
using Windows.Graphics.Imaging;
using Windows.Storage.Streams;
using System.Runtime.InteropServices.WindowsRuntime;
using System.Text.Json;
using System.Text.Json.Nodes;
using NAudio.Wave;

namespace JuiAI.ControlCenter;

public sealed partial class MainWindow
{
    private async Task UiSmoke()
    {
        var directory = Path.Combine(vm.Client.Root, "12_Logs", "ControlCenter-QA", Locale.Language);
        Directory.CreateDirectory(directory);
        var results = new List<object>();
        foreach (var id in new[] { "chat", "history", "agents", "models", "debug", "workspace", "memory", "voice", "voice-processing", "tools", "updates", "settings" })
        {
            await ShowPage(id); await Task.Delay(400);
            await Snapshot(Path.Combine(directory, id + ".png"));
            results.Add(new { page = id, width = Root.ActualWidth, height = Root.ActualHeight, rendered = pageHost.Children.Count > 0 });
        }
        AppWindow.Resize(new Windows.Graphics.SizeInt32(960, 720)); await Task.Delay(300);
        await ShowPage("chat");
        messages!.Children.Clear();
        messages.Children.Add(Controls.MarkdownView.Render(Locale.Text("# JuiAI Terminal\n\nLong text and native Markdown layout test. This paragraph exercises word wrapping in a narrow window.\n\n**Bold**, *italic*, and `inline code`.\n\n- First item\n- Second item\n\n```python\nprint('JuiAI Terminal')\n```\n\n") + new string('W', 180)));
        await Task.Delay(400); await Snapshot(Path.Combine(directory, "chat-narrow-long-text.png"));
        results.Add(new { page = "chat-narrow", width = Root.ActualWidth, height = Root.ActualHeight, rendered = pageHost.Children.Count > 0 });
        await ShowPage("debug"); await Task.Delay(400); await Snapshot(Path.Combine(directory, "debug-narrow.png"));
        results.Add(new { page = "debug-narrow", width = Root.ActualWidth, height = Root.ActualHeight, rendered = pageHost.Children.Count > 0 });
        await ShowPage("voice-processing"); await Task.Delay(400); await Snapshot(Path.Combine(directory, "voice-processing-narrow.png"));
        results.Add(new { page = "voice-processing-narrow", width = Root.ActualWidth, height = Root.ActualHeight, rendered = pageHost.Children.Count > 0 });
        await File.WriteAllTextAsync(Path.Combine(directory, "native-ui-results.json"), JsonSerializer.Serialize(results, new JsonSerializerOptions { WriteIndented = true }));
        AppWindow.Resize(new Windows.Graphics.SizeInt32(1360, 900));
        if (Environment.GetCommandLineArgs().Contains("--voice-smoke")) await VoiceSmoke(directory);
        else { await ShowPage("chat"); ShowInfo(Locale.Text("Native interface checks completed.")); }
    }

    private IEnumerable<string> LocalDebugProfiles(string capability)
    {
        var providers = vm.State["providers"]!.AsArray();
        var localIds = providers.Where(p => p?["enabled"]?.GetValue<bool>() == true && p["local"]?.GetValue<bool>() == true)
            .Select(p => p!["id"]!.ToString()).ToHashSet();
        return vm.State["models"]!.AsArray().Where(m => m?["enabled"]?.GetValue<bool>() == true &&
                localIds.Contains(m["provider_id"]!.ToString()) && m["capabilities"]!.AsArray().Any(c => c?.ToString() == capability))
            .Select(m => m!["id"]!.ToString());
    }

    private async Task VoiceSmoke(string directory)
    {
        var results = new JsonArray();
        byte[]? firstAudio = null;
        int voiceIndex = 0;
        foreach (var model in LocalDebugProfiles("tts").Take(3))
        {
            debugPreset = model; await ShowPage("debug"); await Task.Delay(200);
            var (value, wav) = await debugSpeechSmoke!("Hello, Jui. Let's get started.");
            results.Add(value);
            if (wav is { Length: > 44 })
            {
                firstAudio ??= wav;
                using var stream = new MemoryStream(wav); using var reader = new WaveFileReader(stream);
                await Task.Delay(reader.TotalTime + TimeSpan.FromMilliseconds(300));
            }
            await Snapshot(Path.Combine(directory, $"voice-{++voiceIndex}.png"));
        }
        if (firstAudio != null && voiceIndex > 1)
        {
            results.Add(await debugCompareSmoke!());
            await Snapshot(Path.Combine(directory, "debug-a-b.png"));
        }
        if (firstAudio != null && LocalDebugProfiles("asr").FirstOrDefault() is { } asr)
        {
            debugPreset = asr; await ShowPage("debug");
            results.Add(await debugAsrSmoke!(firstAudio));
            await Snapshot(Path.Combine(directory, "debug-asr-result.png"));
        }
        foreach (var (mode, prompt) in new[] { ("text", "Reply with exactly: Hello, Jui."), ("tools", "Call debug_echo once with the text Hello, Jui.") })
        {
            if (LocalDebugProfiles(mode).FirstOrDefault() is not { } model) continue;
            debugPreset = model; await ShowPage("debug");
            results.Add(await debugTextSmoke!(mode, prompt));
            await Snapshot(Path.Combine(directory, $"debug-{mode}-result.png"));
        }
        await File.WriteAllTextAsync(Path.Combine(directory, "model-debug-live-results.json"), results.ToJsonString(new() { WriteIndented = true }));
        if (results.Count == 0) ShowInfo(Locale.Text("No enabled local model profiles were available for testing."));
        else if (results.All(r => r?["status"]?.ToString().StartsWith(Locale.Text("Completed")) == true)) ShowInfo(Locale.Text("Model tests completed."));
        else ShowError(Locale.Text("Some model tests did not finish. See the local test results."));
    }

    private async Task Snapshot(string path)
    {
        var bitmap = new RenderTargetBitmap(); await bitmap.RenderAsync(Root);
        var pixels = await bitmap.GetPixelsAsync();
        using var stream = new InMemoryRandomAccessStream();
        var encoder = await BitmapEncoder.CreateAsync(BitmapEncoder.PngEncoderId, stream);
        encoder.SetPixelData(BitmapPixelFormat.Bgra8, BitmapAlphaMode.Premultiplied, (uint)bitmap.PixelWidth, (uint)bitmap.PixelHeight, 96, 96, pixels.ToArray());
        await encoder.FlushAsync();
        stream.Seek(0); using var reader = new DataReader(stream.GetInputStreamAt(0)); await reader.LoadAsync((uint)stream.Size);
        var data = new byte[stream.Size]; reader.ReadBytes(data); await File.WriteAllBytesAsync(path, data);
    }
}
