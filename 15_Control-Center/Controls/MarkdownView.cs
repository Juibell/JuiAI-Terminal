using Markdig;
using Markdig.Syntax;
using Markdig.Syntax.Inlines;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Documents;
using Microsoft.UI.Text;
using Windows.ApplicationModel.DataTransfer;
using Windows.UI.Text;

namespace JuiAI.ControlCenter.Controls;

public static class MarkdownView
{
    private static readonly MarkdownPipeline Pipeline = new MarkdownPipelineBuilder().UseAdvancedExtensions().Build();
    public static FrameworkElement Render(string text)
    {
        var panel = new StackPanel { Spacing = 10, HorizontalAlignment = HorizontalAlignment.Stretch };
        foreach (var block in Markdown.Parse(text, Pipeline)) AddBlock(panel, block);
        return panel;
    }
    private static void AddBlock(StackPanel panel, Markdig.Syntax.Block block)
    {
        if (block is CodeBlock code)
        {
            var value = code.Lines.ToString();
            var copy = new Button { Content = new SymbolIcon(Symbol.Copy), HorizontalAlignment = HorizontalAlignment.Right };
            ToolTipService.SetToolTip(copy, "Copy code");
            copy.Click += (_, _) => { var data = new DataPackage(); data.SetText(value); Clipboard.SetContent(data); };
            var content = new StackPanel { Spacing = 4 };
            content.Children.Add(copy);
            content.Children.Add(new TextBlock { Text = value, FontFamily = new Microsoft.UI.Xaml.Media.FontFamily("Cascadia Mono"), FontSize = 13, TextWrapping = TextWrapping.Wrap, IsTextSelectionEnabled = true });
            panel.Children.Add(new Border { Padding = new Thickness(12), CornerRadius = new CornerRadius(4), Background = new Microsoft.UI.Xaml.Media.SolidColorBrush(Microsoft.UI.ColorHelper.FromArgb(255, 35, 35, 35)), Child = content });
        }
        else if (block is LeafBlock leaf && leaf.Inline != null)
        {
            var rich = new RichTextBlock { FontSize = block is HeadingBlock h ? (h.Level <= 2 ? 20 : 17) : 15, TextWrapping = TextWrapping.Wrap, IsTextSelectionEnabled = true };
            var paragraph = new Paragraph();
            AddInlines(paragraph.Inlines, leaf.Inline);
            rich.Blocks.Add(paragraph);
            panel.Children.Add(rich);
        }
        else if (block is ListBlock list)
        {
            int index = 1;
            foreach (var item in list)
            {
                var row = new Grid { ColumnSpacing = 8 };
                row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(24) });
                row.ColumnDefinitions.Add(new ColumnDefinition());
                row.Children.Add(new TextBlock { Text = list.IsOrdered ? (index++) + "." : "•" });
                var body = new StackPanel { Spacing = 4 }; Grid.SetColumn(body, 1);
                if (item is ContainerBlock container) foreach (var child in container) AddBlock(body, child);
                row.Children.Add(body); panel.Children.Add(row);
            }
        }
        else if (block is ContainerBlock container)
        {
            foreach (var child in container) AddBlock(panel, child);
        }
    }
    private static void AddInlines(InlineCollection target, ContainerInline container)
    {
        foreach (var inline in container)
        {
            switch (inline)
            {
                case LiteralInline literal: target.Add(new Run { Text = literal.Content.ToString() }); break;
                case CodeInline code: target.Add(new Run { Text = code.Content, FontFamily = new Microsoft.UI.Xaml.Media.FontFamily("Cascadia Mono") }); break;
                case LineBreakInline: target.Add(new LineBreak()); break;
                case EmphasisInline emphasis:
                    var span = new Span();
                    if (emphasis.DelimiterCount >= 2) span.FontWeight = FontWeights.SemiBold;
                    else span.FontStyle = FontStyle.Italic;
                    AddInlines(span.Inlines, emphasis); target.Add(span); break;
                case LinkInline link:
                    if (Uri.TryCreate(link.Url, UriKind.Absolute, out var uri) && uri.Scheme is "https" or "http" && !link.IsImage)
                    {
                        var hyperlink = new Hyperlink { NavigateUri = uri }; AddInlines(hyperlink.Inlines, link); target.Add(hyperlink);
                    }
                    else AddInlines(target, link);
                    break;
                case ContainerInline nested: AddInlines(target, nested); break;
                case AutolinkInline autoLink: target.Add(new Run { Text = autoLink.Url }); break;
            }
        }
    }
}
