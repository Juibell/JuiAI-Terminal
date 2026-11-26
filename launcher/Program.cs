using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Threading.Tasks;
using System.Windows.Forms;

[assembly: AssemblyTitle("JuiAI Terminal")]
[assembly: AssemblyProduct("JuiAI Terminal")]
[assembly: AssemblyDescription("Launch the JuiAI Terminal desktop application")]

internal static class Program
{
    [STAThread]
    private static int Main()
    {
        try
        {
            string root = AppDomain.CurrentDomain.BaseDirectory;
            string script = Path.Combine(root, "Start-JuiAI.ps1");
            if (!File.Exists(script))
                throw new FileNotFoundException("Extract the entire JuiAI Terminal ZIP into one folder before opening the application.");
            if (File.Exists(Path.Combine(root, @"05_Agent-Router\.runtime\initialized-root.txt")))
                StartTerminal(root, script);
            else
                ShowSetupProgress(root, script);
            return 0;
        }
        catch (Exception ex)
        {
            MessageBox.Show(ex.Message, "JuiAI Terminal", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }

    private static void ShowSetupProgress(string root, string script)
    {
        Application.EnableVisualStyles();
        using (var window = new Form())
        {
            window.Text = "JuiAI Terminal";
            window.Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);
            window.StartPosition = FormStartPosition.CenterScreen;
            window.FormBorderStyle = FormBorderStyle.FixedDialog;
            window.ControlBox = false;
            window.AutoScaleMode = AutoScaleMode.Dpi;
            window.ClientSize = new Size(400, 94);
            var label = new Label { Left = 16, Top = 12, Width = 372, Height = 40,
                Text = "Preparing the local Python environment.\r\nFirst-time setup may take a few minutes." };
            var progress = new ProgressBar { Left = 16, Top = 61, Width = 368, Height = 12,
                Style = ProgressBarStyle.Marquee };
            window.Controls.Add(label);
            window.Controls.Add(progress);
            Exception failure = null;
            window.Shown += async delegate
            {
                try { await Task.Run(() => StartTerminal(root, script)); }
                catch (Exception ex) { failure = ex; }
                finally { window.Close(); }
            };
            window.ShowDialog();
            if (failure != null) throw failure;
        }
    }

    private static void StartTerminal(string root, string script)
    {
        string logs = Path.Combine(root, @"12_Logs\Workbench");
        Directory.CreateDirectory(logs);
        string log = Path.Combine(logs, "launcher.log");
        string powershell = Path.Combine(Environment.SystemDirectory, @"WindowsPowerShell\v1.0\powershell.exe");
        var start = new ProcessStartInfo(powershell, "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File \"" + script + "\"");
        start.WorkingDirectory = root;
        start.UseShellExecute = false;
        start.CreateNoWindow = true;
        start.RedirectStandardOutput = true;
        start.RedirectStandardError = true;
        // Windows PowerShell reconstructs its own module search path when the inherited value is absent.
        start.EnvironmentVariables.Remove("PSModulePath");
        using (var child = Process.Start(start))
        {
            var stdout = child.StandardOutput.ReadToEndAsync();
            var stderr = child.StandardError.ReadToEndAsync();
            child.WaitForExit();
            string output = stdout.Result;
            string error = stderr.Result;
            File.WriteAllText(log, DateTime.UtcNow.ToString("o") + "  PowerShell exit code " + child.ExitCode
                + Environment.NewLine + output + Environment.NewLine + error);
            if (child.ExitCode == 0) return;
            string reason = error.Trim();
            if (reason.Length == 0) reason = output.Trim();
            if (reason.Length > 1200) reason = reason.Substring(reason.Length - 1200);
            throw new InvalidOperationException("Startup failed (PowerShell exit code " + child.ExitCode + ")."
                + Environment.NewLine + reason + Environment.NewLine + Environment.NewLine + "Log: " + log);
        }
    }
}
