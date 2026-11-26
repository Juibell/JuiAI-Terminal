using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using System.Text.Json.Nodes;
using JuiAI.ControlCenter.ViewModels;

namespace JuiAI.ControlCenter;

public sealed partial class MainWindow
{
    private async Task HistoryPage()
    {
        await vm.RefreshConversations();
        var filter = Field("Search conversations");
        var list = new ListView { SelectionMode = ListViewSelectionMode.Multiple, MinHeight = 240, MaxHeight = 550 };
        void Filter() { list.ItemsSource = vm.Conversations.Where(c => c.Name.Contains(filter.Text, StringComparison.OrdinalIgnoreCase)).ToArray(); }
        filter.TextChanged += (_, _) => Filter(); Filter();
        var commands = Row(Icon(Symbol.SelectAll, "Select all", () => { list.SelectAll(); return Task.CompletedTask; }), Icon(Symbol.ClearSelection, "Clear selection", () => { list.SelectedItems.Clear(); return Task.CompletedTask; }), Icon(Symbol.Delete, "Delete selected conversations", async () =>
        {
            var ids = list.SelectedItems.Cast<Choice>().Select(c => c.Id).ToArray(); if (ids.Length == 0) return;
            if (await Confirm("Delete conversations", $"Permanently delete the {ids.Length} selected conversations and their replies?"))
            {
                await vm.Client.Post("api/v1/conversations/delete", new { ids }); if (ids.Contains(vm.ConversationId)) vm.ConversationId = null; await ShowPage("history");
            }
        }), Icon(Symbol.Download, "Export selected conversations", async () =>
        {
            var rows = new JsonArray(); foreach (var item in list.SelectedItems.Cast<Choice>()) rows.Add(await vm.Client.Get("api/v1/conversations/" + item.Id)); await Export("JuiAI-chats", rows.ToJsonString(new() { WriteIndented = true }));
        }), Icon(Symbol.Edit, "Rename", async () =>
        {
            if (list.SelectedItems.Count != 1) { ShowInfo("Select one conversation."); return; }
            var selected = (Choice)list.SelectedItems[0]; var name = Field("Name", selected.Name);
            if (await Form("Rename", Stack(name))) { await vm.Client.Patch("api/v1/conversations/" + selected.Id, new { title = name.Text }); await ShowPage("history"); }
        }), Icon(Symbol.Favorite, "Toggle favorite", async () =>
        {
            foreach (var selected in list.SelectedItems.Cast<Choice>()) { var value = await vm.Client.Get("api/v1/conversations/" + selected.Id); await vm.Client.Patch("api/v1/conversations/" + selected.Id, new { favorite = !(value["favorite"]?.GetValue<bool>() ?? false) }); } await ShowPage("history");
        }), Icon(Symbol.Message, "Continue conversation", async () =>
        {
            if (list.SelectedItems.FirstOrDefault() is Choice selected) { vm.ConversationId = selected.Id; navigation.SelectedItem = navigation.MenuItems[0]; await ShowPage("chat"); }
        }));
        Scrolled(Stack(filter, commands, list));
    }

    private async Task WorkspacePage()
    {
        var choices = Combo("Current workspace", vm.WorkspaceChoices(), vm.WorkspaceId);
        var policy = vm.State["policies"]!.AsArray().FirstOrDefault(p => p!["id"]!.ToString() == vm.WorkspaceId);
        var cloud = new CheckBox { Content = "Allow this workspace's files, memories, and related history to be sent to configured external models", IsChecked = policy?["allow_cloud_context"]?.GetValue<bool>() ?? false };
        var cloudAudio = new CheckBox { Content = "Allow this workspace to use cloud speech services", IsChecked = policy?["allow_cloud_audio"]?.GetValue<bool>() ?? false };
        var selectedPath = vm.State["workspaces"]!.AsArray().First(w => w!["id"]!.ToString() == vm.WorkspaceId)!["path"]!.ToString();
        var relative = Field("Path relative to the workspace", ".");
        var entries = new ListView { SelectionMode = ListViewSelectionMode.Single, MaxHeight = 260 };
        var preview = new TextBox { IsReadOnly = true, AcceptsReturn = true, TextWrapping = TextWrapping.Wrap, MinHeight = 160, MaxHeight = 400, FontFamily = new Microsoft.UI.Xaml.Media.FontFamily("Cascadia Mono") };
        async Task LoadFiles()
        {
            var value = await vm.Client.Get("api/workspace?path=" + Uri.EscapeDataString(relative.Text));
            entries.ItemsSource = value["items"]!.AsArray().Select(x => new Choice(x!["path"]!.ToString(), (x["directory"]!.GetValue<bool>() ? "[Folder] " : "") + x["path"])).ToArray();
        }
        entries.DoubleTapped += (_, _) => Run(async () =>
        {
            if (entries.SelectedItem is not Choice item) return;
            if (item.Name.StartsWith("[Folder]", StringComparison.Ordinal)) { relative.Text = item.Id; await LoadFiles(); }
            else preview.Text = (await vm.Client.Get("api/workspace/file?path=" + Uri.EscapeDataString(item.Id)))["text"]?.ToString() ?? "";
        });
        var tools = Row(Command("Switch workspace", Symbol.Switch, async () => { await vm.Client.Post("api/workspaces/" + Selected(choices) + "/select"); await ReloadPage(); }), Command("Add folder", Symbol.Add, async () =>
        {
            var folder = await PickFolder(); if (folder == null) return;
            await vm.Client.Post("api/workspaces", new { name = Path.GetFileName(folder), path = folder }); await ReloadPage();
        }), Icon(Symbol.Delete, "Unregister workspace (keep files)", async () => { if (await Confirm("Unregister workspace", "This removes the workspace registration and keeps its files on disk.")) { await vm.Client.Delete("api/workspaces/" + Selected(choices)); await ReloadPage(); } }));
        Scrolled(Stack(choices, Text(selectedPath), tools, cloud, cloudAudio, Command("Save sharing permissions", Symbol.Save, async () =>
        {
            if ((cloud.IsChecked == true || cloudAudio.IsChecked == true) && !await Confirm("External service permissions", "Allow this workspace's content to be sent to your configured external APIs. This permission excludes credentials and unrelated files.")) return;
            await vm.Client.Put($"api/v1/workspaces/{vm.WorkspaceId}/policy", new { allow_cloud_context = cloud.IsChecked == true, allow_cloud_audio = cloudAudio.IsChecked == true }); await vm.Refresh(); ShowInfo("Permissions saved for new tasks.");
        }), Text("Files", 18), relative, Row(Icon(Symbol.Refresh, "List directory", LoadFiles), Icon(Symbol.Home, "Workspace root", async () => { relative.Text = "."; await LoadFiles(); }), Command("Git status", Symbol.List, async () => { preview.Text = (await vm.Client.Get("api/git")).ToJsonString(new() { WriteIndented = true }); })), entries, preview));
        await LoadFiles();
    }

    private void MemoryPage()
    {
        var enabled = new ToggleSwitch { Header = "Memory", OnContent = "On", OffContent = "Off", IsOn = vm.State["memory_enabled"]?.GetValue<bool>() ?? true };
        enabled.Toggled += (_, _) => Run(async () => { await vm.Client.Put("api/preferences/memory", new { enabled = enabled.IsOn }); await vm.Refresh(); });
        var body = Stack(enabled, Command("Add memory", Symbol.Add, () => MemoryDialog()));
        foreach (var item in vm.State["memories"]!.AsArray())
        {
            var value = item!;
            body.Children.Add(ItemRow(Stack(Text((value["pinned"]?.GetValue<bool>() == true ? "Pinned · " : "") + value["title"], 16), Text(value["content"]!.ToString()), Text($"{value["category"]} · {(value["workspace_id"] == null ? "Global" : "Project")} · {(value["enabled"]?.GetValue<bool>() == true ? "Enabled" : "Disabled")}")), Row(Icon(Symbol.Edit, "Edit memory", () => MemoryDialog(value)), Icon(Symbol.Delete, "Delete memory", async () => { if (await Confirm("Delete memory", value["title"]!.ToString())) { await vm.Client.Delete("api/memories/" + value["id"]); await ReloadPage(); } }))));
        }
        Scrolled(body);
    }
    private Task SaveMemoryFromText(string text) => MemoryDialog(null, text);
    private async Task MemoryDialog(JsonNode? old = null, string? content = null)
    {
        var title = Field("Title", old?["title"]?.ToString() ?? "");
        var body = Field("Content", old?["content"]?.ToString() ?? content?[..Math.Min(2000, content.Length)] ?? "", true); body.MaxLength = 2000;
        var category = Combo("Type", new[] { new Choice("fact", "Fact"), new("preference", "Preference"), new("project", "Project") }, old?["category"]?.ToString());
        var workspace = Combo("Scope", new[] { new Choice("", "Global") }.Concat(vm.WorkspaceChoices()), old?["workspace_id"]?.ToString() ?? (content == null ? "" : vm.WorkspaceId));
        var enabled = new CheckBox { Content = "Enable", IsChecked = old?["enabled"]?.GetValue<bool>() ?? true }; var pinned = new CheckBox { Content = "Pin", IsChecked = old?["pinned"]?.GetValue<bool>() ?? false };
        if (!await Form("Memory", Stack(title, body, category, workspace, enabled, pinned))) return;
        var value = new { title = title.Text, content = body.Text, category = Selected(category), workspace_id = Selected(workspace) is "" ? null : Selected(workspace), enabled = enabled.IsChecked == true, pinned = pinned.IsChecked == true };
        if (old == null) await vm.Client.Post("api/memories", value); else await vm.Client.Put("api/memories/" + old["id"], value);
        await vm.Refresh(); if (page == "memory") await ShowPage("memory"); else ShowInfo("Memory saved.");
    }
}
