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
        var filter = Field(Locale.Text("Search conversations"));
        var list = new ListView { SelectionMode = ListViewSelectionMode.Multiple, MinHeight = 240, MaxHeight = 550 };
        void Filter() { list.ItemsSource = vm.Conversations.Where(c => c.Name.Contains(filter.Text, StringComparison.OrdinalIgnoreCase)).ToArray(); }
        filter.TextChanged += (_, _) => Filter(); Filter();
        var commands = Row(Icon(Symbol.SelectAll, Locale.Text("Select all"), () => { list.SelectAll(); return Task.CompletedTask; }), Icon(Symbol.ClearSelection, Locale.Text("Clear selection"), () => { list.SelectedItems.Clear(); return Task.CompletedTask; }), Icon(Symbol.Delete, Locale.Text("Delete selected conversations"), async () =>
        {
            var ids = list.SelectedItems.Cast<Choice>().Select(c => c.Id).ToArray(); if (ids.Length == 0) return;
            if (await Confirm(Locale.Text("Delete conversations"), Locale.Format("Permanently delete the {0} selected conversations and their replies?", ids.Length)))
            {
                await vm.Client.Post("api/v1/conversations/delete", new { ids }); if (ids.Contains(vm.ConversationId)) vm.ConversationId = null; await ShowPage("history");
            }
        }), Icon(Symbol.Download, Locale.Text("Export selected conversations"), async () =>
        {
            var rows = new JsonArray(); foreach (var item in list.SelectedItems.Cast<Choice>()) rows.Add(await vm.Client.Get("api/v1/conversations/" + item.Id)); await Export("JuiAI-chats", rows.ToJsonString(new() { WriteIndented = true }));
        }), Icon(Symbol.Edit, Locale.Text("Rename"), async () =>
        {
            if (list.SelectedItems.Count != 1) { ShowInfo(Locale.Text("Select one conversation.")); return; }
            var selected = (Choice)list.SelectedItems[0];
            var conversation = await vm.Client.Get("api/v1/conversations/" + selected.Id);
            var placeholder = conversation["title_is_placeholder"]?.GetValue<bool>() == true;
            var name = Field(Locale.Text("Name"), placeholder ? "" : conversation["title"]?.ToString() ?? "");
            if (placeholder) name.PlaceholderText = Locale.Text("New chat");
            if (await Form(Locale.Text("Rename"), Stack(name)) && !string.IsNullOrWhiteSpace(name.Text))
            {
                await vm.Client.Patch("api/v1/conversations/" + selected.Id, new { title = name.Text });
                await ShowPage("history");
            }
        }), Icon(Symbol.Favorite, Locale.Text("Toggle favorite"), async () =>
        {
            foreach (var selected in list.SelectedItems.Cast<Choice>()) { var value = await vm.Client.Get("api/v1/conversations/" + selected.Id); await vm.Client.Patch("api/v1/conversations/" + selected.Id, new { favorite = !(value["favorite"]?.GetValue<bool>() ?? false) }); } await ShowPage("history");
        }), Icon(Symbol.Message, Locale.Text("Continue conversation"), async () =>
        {
            if (list.SelectedItems.FirstOrDefault() is Choice selected) { vm.ConversationId = selected.Id; navigation.SelectedItem = navigation.MenuItems[0]; await ShowPage("chat"); }
        }));
        Scrolled(Stack(filter, commands, list));
    }

    private async Task WorkspacePage()
    {
        var choices = Combo(Locale.Text("Current workspace"), vm.WorkspaceChoices(), vm.WorkspaceId);
        var policy = vm.State["policies"]!.AsArray().FirstOrDefault(p => p!["id"]!.ToString() == vm.WorkspaceId);
        var cloud = new CheckBox { Content = Locale.Text("Allow this workspace's files, memories, and related history to be sent to configured external models"), IsChecked = policy?["allow_cloud_context"]?.GetValue<bool>() ?? false };
        var cloudAudio = new CheckBox { Content = Locale.Text("Allow this workspace to use cloud speech services"), IsChecked = policy?["allow_cloud_audio"]?.GetValue<bool>() ?? false };
        var selectedPath = vm.State["workspaces"]!.AsArray().First(w => w!["id"]!.ToString() == vm.WorkspaceId)!["path"]!.ToString();
        var relative = Field(Locale.Text("Path relative to the workspace"), ".");
        var entries = new ListView { SelectionMode = ListViewSelectionMode.Single, MaxHeight = 260 };
        var preview = new TextBox { IsReadOnly = true, AcceptsReturn = true, TextWrapping = TextWrapping.Wrap, MinHeight = 160, MaxHeight = 400, FontFamily = new Microsoft.UI.Xaml.Media.FontFamily("Cascadia Mono") };
        async Task LoadFiles()
        {
            var value = await vm.Client.Get("api/workspace?path=" + Uri.EscapeDataString(relative.Text));
            entries.ItemsSource = value["items"]!.AsArray().Select(x => new Choice(x!["path"]!.ToString(), (x["directory"]!.GetValue<bool>() ? Locale.Text("[Folder] ") : "") + x["path"])).ToArray();
        }
        entries.DoubleTapped += (_, _) => Run(async () =>
        {
            if (entries.SelectedItem is not Choice item) return;
            if (item.Name.StartsWith(Locale.Text("[Folder]"), StringComparison.Ordinal)) { relative.Text = item.Id; await LoadFiles(); }
            else preview.Text = (await vm.Client.Get("api/workspace/file?path=" + Uri.EscapeDataString(item.Id)))["text"]?.ToString() ?? "";
        });
        var tools = Row(Command(Locale.Text("Switch workspace"), Symbol.Switch, async () => { await vm.Client.Post("api/workspaces/" + Selected(choices) + "/select"); await ReloadPage(); }), Command(Locale.Text("Add folder"), Symbol.Add, async () =>
        {
            var folder = await PickFolder(); if (folder == null) return;
            await vm.Client.Post("api/workspaces", new { name = Path.GetFileName(folder), path = folder }); await ReloadPage();
        }), Icon(Symbol.Delete, Locale.Text("Unregister workspace (keep files)"), async () => { if (await Confirm(Locale.Text("Unregister workspace"), Locale.Text("This removes the workspace registration and keeps its files on disk."))) { await vm.Client.Delete("api/workspaces/" + Selected(choices)); await ReloadPage(); } }));
        Scrolled(Stack(choices, Text(selectedPath), tools, cloud, cloudAudio, Command(Locale.Text("Save sharing permissions"), Symbol.Save, async () =>
        {
            if ((cloud.IsChecked == true || cloudAudio.IsChecked == true) && !await Confirm(Locale.Text("External service permissions"), Locale.Text("Allow this workspace's content to be sent to your configured external APIs. This permission excludes credentials and unrelated files."))) return;
            await vm.Client.Put($"api/v1/workspaces/{vm.WorkspaceId}/policy", new { allow_cloud_context = cloud.IsChecked == true, allow_cloud_audio = cloudAudio.IsChecked == true }); await vm.Refresh(); ShowInfo(Locale.Text("Permissions saved for new tasks."));
        }), Text(Locale.Text("Files"), 18), relative, Row(Icon(Symbol.Refresh, Locale.Text("List directory"), LoadFiles), Icon(Symbol.Home, Locale.Text("Workspace root"), async () => { relative.Text = "."; await LoadFiles(); }), Command(Locale.Text("Git status"), Symbol.List, async () => { preview.Text = (await vm.Client.Get("api/git")).ToJsonString(new() { WriteIndented = true }); })), entries, preview));
        await LoadFiles();
    }

    private void MemoryPage()
    {
        var enabled = new ToggleSwitch { OnContent = Locale.Text("On"), OffContent = Locale.Text("Off"), Header = Locale.Text("Memory"), IsOn = vm.State["memory_enabled"]?.GetValue<bool>() ?? true };
        enabled.Toggled += (_, _) => Run(async () => { await vm.Client.Put("api/preferences/memory", new { enabled = enabled.IsOn }); await vm.Refresh(); });
        var body = Stack(enabled, Command(Locale.Text("Add memory"), Symbol.Add, () => MemoryDialog()));
        foreach (var item in vm.State["memories"]!.AsArray())
        {
            var value = item!;
            body.Children.Add(ItemRow(Stack(Text((value["pinned"]?.GetValue<bool>() == true ? Locale.Text("Pinned · ") : "") + value["title"], 16), Text(value["content"]!.ToString()), Text($"{Locale.Status(value["category"]?.ToString())} · {(value["workspace_id"] == null ? Locale.Text("Global") : Locale.Text("Project"))} · {(value["enabled"]?.GetValue<bool>() == true ? Locale.Text("Enabled") : Locale.Text("Disabled"))}")), Row(Icon(Symbol.Edit, Locale.Text("Edit memory"), () => MemoryDialog(value)), Icon(Symbol.Delete, Locale.Text("Delete memory"), async () => { if (await Confirm(Locale.Text("Delete memory"), value["title"]!.ToString())) { await vm.Client.Delete("api/memories/" + value["id"]); await ReloadPage(); } }))));
        }
        Scrolled(body);
    }
    private Task SaveMemoryFromText(string text) => MemoryDialog(null, text);
    private async Task MemoryDialog(JsonNode? old = null, string? content = null)
    {
        var title = Field(Locale.Text("Title"), old?["title"]?.ToString() ?? "");
        var body = Field(Locale.Text("Content"), old?["content"]?.ToString() ?? content?[..Math.Min(2000, content.Length)] ?? "", true); body.MaxLength = 2000;
        var category = Combo(Locale.Text("Type"), new[] { new Choice("fact", Locale.Text("Fact")), new("preference", Locale.Text("Preference")), new("project", Locale.Text("Project")) }, old?["category"]?.ToString());
        var workspace = Combo(Locale.Text("Scope"), new[] { new Choice("", Locale.Text("Global")) }.Concat(vm.WorkspaceChoices()), old?["workspace_id"]?.ToString() ?? (content == null ? "" : vm.WorkspaceId));
        var enabled = new CheckBox { Content = Locale.Text("Enable"), IsChecked = old?["enabled"]?.GetValue<bool>() ?? true }; var pinned = new CheckBox { Content = Locale.Text("Pin"), IsChecked = old?["pinned"]?.GetValue<bool>() ?? false };
        if (!await Form(Locale.Text("Memory"), Stack(title, body, category, workspace, enabled, pinned))) return;
        var value = new { title = title.Text, content = body.Text, category = Selected(category), workspace_id = Selected(workspace) is "" ? null : Selected(workspace), enabled = enabled.IsChecked == true, pinned = pinned.IsChecked == true };
        if (old == null) await vm.Client.Post("api/memories", value); else await vm.Client.Put("api/memories/" + old["id"], value);
        await vm.Refresh(); if (page == "memory") await ShowPage("memory"); else ShowInfo(Locale.Text("Memory saved."));
    }
}
