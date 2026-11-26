using Microsoft.UI.Xaml;
using Microsoft.Windows.AppLifecycle;
using System.Text.Json;

namespace JuiAI.ControlCenter;

public partial class App : Application
{
    private Window? window;
    public App()
    {
        RequestedTheme = ApplicationTheme.Dark;
        InitializeComponent();
        UnhandledException += (_, error) => RecordFailure(error.Exception);
    }
    protected override async void OnLaunched(LaunchActivatedEventArgs args)
    {
        try
        {
            var instance = AppInstance.FindOrRegisterForKey("JuiAI.Terminal");
            if (!instance.IsCurrent)
            {
                await instance.RedirectActivationToAsync(AppInstance.GetCurrent().GetActivatedEventArgs());
                Exit(); return;
            }
            window = new MainWindow();
            instance.Activated += (_, _) => window.DispatcherQueue.TryEnqueue(() => { window.AppWindow.Show(); window.Activate(); });
            window.Activate();
        }
        catch (Exception error) { RecordFailure(error); Exit(); }
    }
    private static void RecordFailure(Exception error)
    {
        var directory = Path.Combine(Services.JuiClient.FindRoot(), "12_Logs", "Workbench");
        Directory.CreateDirectory(directory);
        File.AppendAllText(Path.Combine(directory, "native-client.stderr.log"), DateTimeOffset.UtcNow + " " + error + Environment.NewLine);
    }
}
