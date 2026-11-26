using System.Runtime.InteropServices;

namespace JuiAI.ControlCenter.Services;

public sealed class TrayIcon : IDisposable
{
    private const uint ImageIcon = 1;
    private const uint LoadFromFile = 0x0010;
    private const int SmallIconWidth = 49;
    private const int SmallIconHeight = 50;
    private readonly nint window;
    private readonly nint ownedIcon;
    private bool disposed;
    private readonly WindowProcedure procedure;
    private readonly Action show;
    private readonly Action exit;
    private bool visible;
    private readonly string className = "JuiAITray" + Environment.ProcessId;
    private delegate nint WindowProcedure(nint hwnd, uint message, nuint wparam, nint lparam);
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct WindowClass
    {
        public uint style; public WindowProcedure procedure; public int classExtra; public int windowExtra; public nint instance; public nint icon; public nint cursor; public nint background; public string? menuName; public string className;
    }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct NotifyData
    {
        public uint size; public nint window; public uint id; public uint flags; public uint message; public nint icon;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string tip;
        public uint state; public uint stateMask;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string info;
        public uint timeout;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 64)] public string infoTitle;
        public uint infoFlags; public Guid guid; public nint balloonIcon;
    }
    public TrayIcon(nint owner, string iconPath, Action show, Action exit)
    {
        this.show = show; this.exit = exit;
        uint dpi = GetDpiForWindow(owner);
        if (dpi == 0) dpi = 96;
        // Load a private HICON at the notification area's small-icon size.
        ownedIcon = LoadImage(0, iconPath, ImageIcon, GetSystemMetricsForDpi(SmallIconWidth, dpi), GetSystemMetricsForDpi(SmallIconHeight, dpi), LoadFromFile);
        if (ownedIcon == 0) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error(), "Could not load the JuiAI Terminal notification icon.");
        procedure = Handle;
        var type = new WindowClass { procedure = procedure, className = className, instance = GetModuleHandle(null) };
        if (RegisterClass(ref type) == 0)
        {
            int error = Marshal.GetLastWin32Error();
            DestroyIcon(ownedIcon);
            throw new System.ComponentModel.Win32Exception(error, "Could not register the notification window.");
        }
        window = CreateWindowEx(0, className, "", 0, 0, 0, 0, 0, new nint(-3), 0, type.instance, 0);
        if (window == 0)
        {
            int error = Marshal.GetLastWin32Error();
            UnregisterClass(className, type.instance);
            DestroyIcon(ownedIcon);
            throw new System.ComponentModel.Win32Exception(error, "Could not create the notification window.");
        }
    }
    private NotifyData Data() => new() { size = (uint)Marshal.SizeOf<NotifyData>(), window = window, id = 1, flags = 1 | 2 | 4, message = 0x8001, icon = ownedIcon, tip = "JuiAI Terminal", info = "", infoTitle = "" };
    public void Show() { if (visible || disposed) return; var data = Data(); visible = Shell_NotifyIcon(0, ref data); }
    private nint Handle(nint hwnd, uint message, nuint wparam, nint lparam)
    {
        if (message == 0x8001)
        {
            var action = (uint)(lparam.ToInt64() & 0xFFFF);
            if (action is 0x0202 or 0x0203 or 0x0400) { show(); return 0; }
            if (action == 0x0205) { show(); exit(); return 0; }
        }
        return DefWindowProc(hwnd, message, wparam, lparam);
    }
    public void Dispose()
    {
        if (disposed) return;
        disposed = true;
        if (visible) { var data = Data(); Shell_NotifyIcon(2, ref data); }
        DestroyWindow(window);
        UnregisterClass(className, GetModuleHandle(null));
        DestroyIcon(ownedIcon);
    }
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)] private static extern ushort RegisterClass(ref WindowClass value);
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)] private static extern nint CreateWindowEx(uint ex, string name, string title, uint style, int x, int y, int width, int height, nint parent, nint menu, nint instance, nint parameter);
    [DllImport("user32.dll")] private static extern bool DestroyWindow(nint hwnd);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern bool UnregisterClass(string name, nint instance);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern nint DefWindowProc(nint hwnd, uint message, nuint wparam, nint lparam);
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)] private static extern nint LoadImage(nint instance, string path, uint type, int width, int height, uint flags);
    [DllImport("user32.dll")] private static extern uint GetDpiForWindow(nint hwnd);
    [DllImport("user32.dll")] private static extern int GetSystemMetricsForDpi(int index, uint dpi);
    [DllImport("user32.dll")] private static extern bool DestroyIcon(nint icon);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] private static extern nint GetModuleHandle(string? name);
    [DllImport("shell32.dll", CharSet = CharSet.Unicode)] private static extern bool Shell_NotifyIcon(uint message, ref NotifyData data);
}
