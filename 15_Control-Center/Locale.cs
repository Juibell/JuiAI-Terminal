using System.Globalization;
using System.Text;
using System.Text.Json;

namespace JuiAI.ControlCenter;

internal static class Locale
{
    public const string English = "en-US";
    public const string Chinese = "zh-CN";
    public static string Language { get; private set; } = English;

    private static readonly Lazy<Dictionary<string, string>> Translations = new(() =>
    {
        using var resource = typeof(Locale).Assembly.GetManifestResourceStream("JuiAI.Terminal.zh-CN.json")
            ?? throw new InvalidOperationException("Missing Simplified Chinese UI resources.");
        return JsonSerializer.Deserialize<Dictionary<string, string>>(resource)!;
    });

    private static string PreferencePath(string root) =>
        Path.Combine(root, "05_Agent-Router", ".runtime", "ui-language.txt");

    public static void Load(string root)
    {
        var path = PreferencePath(root);
        var language = File.Exists(path) ? File.ReadAllText(path).Trim() : English;
        Apply(language == Chinese ? Chinese : English);
    }

    public static void Save(string root, string language)
    {
        if (language is not (English or Chinese)) throw new ArgumentOutOfRangeException(nameof(language));
        var path = PreferencePath(root);
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        var temporary = path + "." + Guid.NewGuid().ToString("N");
        try
        {
            File.WriteAllText(temporary, language, new UTF8Encoding(false));
            File.Move(temporary, path, true);
        }
        finally { File.Delete(temporary); }
        Apply(language);
    }

    private static void Apply(string language)
    {
        Language = language;
        var culture = CultureInfo.GetCultureInfo(language);
        CultureInfo.DefaultThreadCurrentUICulture = culture;
        CultureInfo.CurrentUICulture = culture;
    }

    // Call only with application-owned labels. User text and provider output pass through unchanged.
    public static string Text(string label) => Language == Chinese && Translations.Value.TryGetValue(label, out var text) ? text : label;
    public static string Format(string label, params object?[] values) =>
        string.Format(CultureInfo.GetCultureInfo(Language), Text(label), values);
    public static string Status(string? value) => value is null ? "" : Text(value);
}
