using System.ComponentModel;
using System.Runtime.CompilerServices;
using System.Collections.ObjectModel;
using System.Text.Json.Nodes;
using JuiAI.ControlCenter.Services;

namespace JuiAI.ControlCenter.ViewModels;

public sealed record Choice(string Id, string Name)
{
    public override string ToString() => Name;
}

public sealed class ControlViewModel : INotifyPropertyChanged
{
    public JuiClient Client { get; }
    public ControlViewModel(JuiClient? client = null) => Client = client ?? new JuiClient();
    public JsonNode State { get; private set; } = new JsonObject();
    public ObservableCollection<Choice> Conversations { get; } = [];
    public string? ConversationId { get; set; }
    public string? RunId { get; set; }
    private string status = Locale.Text("Connecting");
    public string Status { get => status; set { status = value; Changed(); } }
    public string WorkspaceId => State["workspace_id"]?.ToString() ?? "default";
    public string? MainModelId => State["roles"]?["bindings"]?["main"]?.ToString();
    public string MainModelName => State["models"]?.AsArray()
        .FirstOrDefault(model => model?["id"]?.ToString() == MainModelId)?["name"]?.ToString()
        ?? MainModelId ?? Locale.Text("Unconfigured");
    public event PropertyChangedEventHandler? PropertyChanged;
    private void Changed([CallerMemberName] string? name = null) => PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
    public async Task Refresh()
    {
        State = await Client.Get("api/v1/state");
        Changed(nameof(State));
    }
    public async Task SetMainModel(string modelId)
    {
        State["roles"] = await Client.Patch("api/v1/roles/main", new { model_ref = modelId });
        Changed(nameof(State));
    }
    public async Task RefreshConversations()
    {
        var items = (await Client.Get("api/v1/conversations")).AsArray().OrderByDescending(x => x?["favorite"]?.GetValue<bool>() ?? false).ThenByDescending(x => x?["updated"]?.ToString()).ToList();
        Conversations.Clear();
        foreach (var item in items)
        {
            var title = item!["title_is_placeholder"]?.GetValue<bool>() == true ? Locale.Text("New chat") : item["title"]?.ToString();
            var prefix = item["favorite"]?.GetValue<bool>() == true ? "★ " : "";
            Conversations.Add(new Choice(item["id"]!.ToString(), prefix + title));
        }
    }
    public IEnumerable<Choice> ModelChoices(string? capability = null) => State["models"]?.AsArray().Where(m => m?["enabled"]?.GetValue<bool>() == true && (capability == null || m["capabilities"]!.AsArray().Any(x => x?.ToString() == capability))).Select(m => new Choice(m!["id"]!.ToString(), m["name"]!.ToString())) ?? [];
    public static string WorkspaceName(JsonNode? workspace, string fallback)
    {
        string? name = workspace?["name"]?.ToString();
        if (workspace?["id"]?.ToString() == "default" && name == "Jui Workspace") return Locale.Text("Jui Workspace");
        return name ?? fallback;
    }

    public IEnumerable<Choice> WorkspaceChoices() => State["workspaces"]?.AsArray().Select(w => new Choice(w!["id"]!.ToString(), WorkspaceName(w, w["id"]!.ToString()))) ?? [];
}
