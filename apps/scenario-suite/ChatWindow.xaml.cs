using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Windows;
using System.Windows.Controls;

namespace ScenarioSuite;

public class ChatMessage
{
    public string Text { get; set; } = "";
    public string AutomationId { get; set; } = "";
    public override string ToString() => Text;
}

public class Conversation
{
    public string Name { get; set; } = "";
    public List<ChatMessage> Messages { get; } = new();
    public override string ToString() => Name;
}

/// <summary>
/// A chat screen: a conversation list, a scrollable message history, a
/// self-drawn composer and a send log.
///
/// The composer is a <see cref="SelfDrawnEditor"/> - a control rendered by hand
/// that publishes no writable UIA interface (no ValuePattern, no element HWND,
/// reported as a Group). This is the QQ/豆包 ProseMirror shape the capability
/// report measured, and it is what forces <c>set_text</c> down the fallback
/// chain: the runtime must either refuse honestly or use a synthetic channel the
/// host explicitly enabled.
///
/// Every send appends a line to the outbox, so "the message really went out"
/// is verifiable from the observation, without reading process internals.
/// </summary>
public partial class ChatWindow : Window
{
    private readonly List<Conversation> _conversations = new();
    public ObservableCollection<ChatMessage> VisibleMessages { get; } = new();
    public ObservableCollection<string> OutboxLines { get; } = new();

    public ChatWindow()
    {
        InitializeComponent();
        for (int c = 0; c < 12; c++)
        {
            var conv = new Conversation { Name = $"群组 {c + 1}" };
            for (int m = 0; m < 8; m++)
            {
                conv.Messages.Add(new ChatMessage
                {
                    Text = $"历史消息 {m + 1} @群组{c + 1}",
                    AutomationId = $"chat_history_{c}_{m}",
                });
            }
            _conversations.Add(conv);
        }
        Conversations.ItemsSource = _conversations;
        Messages.ItemsSource = VisibleMessages;
        Outbox.ItemsSource = OutboxLines;
        Conversations.SelectedIndex = 0;
    }

    private Conversation? Current =>
        Conversations.SelectedItem as Conversation;

    private void OnConversationSelected(object sender, SelectionChangedEventArgs e)
    {
        VisibleMessages.Clear();
        if (Current is not { } conv)
        {
            return;
        }
        ConversationTitle.Text = conv.Name;
        foreach (var message in conv.Messages)
        {
            VisibleMessages.Add(message);
        }
    }

    private void OnSendClick(object sender, RoutedEventArgs e)
    {
        var text = Composer.Text.Trim();
        if (Current is not { } conv)
        {
            OutboxLines.Add("（无会话）");
            return;
        }
        if (text.Length == 0)
        {
            OutboxLines.Add("（空消息未发送）");
            return;
        }
        conv.Messages.Add(new ChatMessage
        {
            Text = text,
            AutomationId = $"chat_sent_{conv.Messages.Count}",
        });
        VisibleMessages.Add(conv.Messages[^1]);
        OutboxLines.Add($"{conv.Name}: 已发送 \"{text}\"");
        Composer.Text = "";
    }
}
