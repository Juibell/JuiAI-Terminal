using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
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

            string powershell = Path.Combine(Environment.SystemDirectory, @"WindowsPowerShell\v1.0\powershell.exe");
            var start = new ProcessStartInfo(powershell, "-NoLogo -NoProfile -ExecutionPolicy Bypass -File \"" + script + "\"");
            start.WorkingDirectory = root;
            start.UseShellExecute = false;
            start.CreateNoWindow = File.Exists(Path.Combine(root, @"05_Agent-Router\.runtime\initialized-root.txt"));
            start.RedirectStandardError = start.CreateNoWindow;
            using (var child = Process.Start(start))
            {
                string error = start.RedirectStandardError ? child.StandardError.ReadToEnd() : "";
                child.WaitForExit();
                if (child.ExitCode != 0)
                    throw new InvalidOperationException(String.IsNullOrWhiteSpace(error)
                        ? "Startup failed. Open JuiAI-Terminal.cmd to see the setup output."
                        : error.Trim());
            }
            return 0;
        }
        catch (Exception ex)
        {
            MessageBox.Show(ex.Message, "JuiAI Terminal", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
