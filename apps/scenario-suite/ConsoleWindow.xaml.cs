using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;

namespace ScenarioSuite;

public class Contact
{
    public string Name { get; set; } = "";
    public List<string> Thread { get; } = new();
    public override string ToString() => Name;
}

/// <summary>
/// A chat console with a plain <see cref="TextBox"/> composer.
///
/// Where <see cref="ChatWindow"/> covers the self-drawn editor (no native write
/// interface at all), this screen covers the ordinary text box: it publishes a
/// Value pattern, so the runtime must write through it and never fall back to
/// the clipboard or the keyboard. Having both screens is what makes the routing
/// decision observable rather than assumed.
/// </summary>
public partial class ConsoleWindow : Window
{
    private readonly List<Contact> _contacts = new();
    public ObservableCollection<string> VisibleThread { get; } = new();

    public ConsoleWindow()
    {
        InitializeComponent();
        for (int i = 0; i < 10; i++)
        {
            var contact = new Contact { Name = $"联系人 {i + 1}" };
            contact.Thread.Add($"（与 {contact.Name} 的历史消息）");
            _contacts.Add(contact);
        }
        Contacts.ItemsSource = _contacts;
        Thread.ItemsSource = VisibleThread;
        Contacts.SelectedIndex = 0;
    }

    private Contact? Current => Contacts.SelectedItem as Contact;

    private void OnContactSelected(object sender, SelectionChangedEventArgs e)
    {
        VisibleThread.Clear();
        if (Current is not { } contact)
        {
            return;
        }
        ChatTitle.Text = contact.Name;
        foreach (var line in contact.Thread)
        {
            VisibleThread.Add(line);
        }
    }

    private void OnInputKeyDown(object sender, KeyEventArgs e)
    {
        if (e.Key == Key.Enter)
        {
            Send();
        }
    }

    private void OnSendClick(object sender, RoutedEventArgs e) => Send();

    private void Send()
    {
        var text = Input.Text.Trim();
        if (Current is not { } contact)
        {
            Status.Text = "（无会话）";
            return;
        }
        if (text.Length == 0)
        {
            Status.Text = "（空消息未发送）";
            return;
        }
        contact.Thread.Add($"我: {text}");
        VisibleThread.Add($"我: {text}");
        // The status line is the send record: it names what was sent, so the
        // send is verifiable from the observation alone.
        Status.Text = $"{contact.Name}: 已发送 \"{text}\"";
        Input.Text = "";
    }
}
