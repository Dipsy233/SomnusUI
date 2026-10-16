using System.Linq;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;

namespace ScenarioSuite;

/// <summary>
/// A screen whose destructive action is gated behind a modal confirmation.
///
/// Delete does not remove anything itself: it opens a modal dialog whose
/// confirm/cancel buttons are the only way forward. That dialog is a real
/// separate window (a WPF <see cref="Window"/> shown with <c>ShowDialog</c>),
/// so it appears in the accessibility tree as its own top-level window while it
/// is up - the shape a real confirmation takes, and the case an agent has to
/// discover rather than guess at.
///
/// The status line names what happened (deleted / cancelled), so the outcome is
/// verifiable from the observation.
/// </summary>
public partial class DialogWindow : Window
{
    public DialogWindow()
    {
        InitializeComponent();
        Rows.ItemsSource = new[] { "报告 A", "报告 B", "报告 C" };
    }

    private void OnDeleteClick(object sender, RoutedEventArgs e)
    {
        var confirm = new ConfirmDialog
        {
            Owner = this,
            WindowStartupLocation = WindowStartupLocation.CenterOwner,
        };
        bool? result = confirm.ShowDialog();
        if (result == true)
        {
            var items = Rows.ItemsSource.Cast<string>().ToList();
            if (items.Count > 0)
            {
                items.RemoveAt(items.Count - 1);
                Rows.ItemsSource = items;
            }
            StatusText.Text = "已删除";
        }
        else
        {
            StatusText.Text = "已取消";
        }
    }
}

/// <summary>
/// The confirmation itself: a modal window with a message and two buttons.
///
/// It is a real window rather than an overlay so it lands in the accessibility
/// tree the way a platform dialog does, with its own refs and its own lifetime -
/// the buttons only exist while it is open.
/// </summary>
public partial class ConfirmDialog : Window
{
    public ConfirmDialog()
    {
        Title = "确认删除";
        Width = 320;
        Height = 160;
        WindowStyle = WindowStyle.ToolWindow;
        AutomationProperties.SetAutomationId(this, "dialog_confirm_window");
        AutomationProperties.SetName(this, "确认删除");

        var message = new TextBlock
        {
            Text = "确定要删除最后一项吗？",
            Margin = new Thickness(16, 20, 16, 0),
        };
        AutomationProperties.SetAutomationId(message, "dialog_message");

        var confirm = new Button
        {
            Content = "确定",
            Width = 80,
            Margin = new Thickness(8, 0, 0, 0),
            IsDefault = true,
        };
        AutomationProperties.SetAutomationId(confirm, "dialog_confirm");
        confirm.Click += (_, _) => DialogResult = true;

        var cancel = new Button
        {
            Content = "取消",
            Width = 80,
            IsCancel = true,
        };
        AutomationProperties.SetAutomationId(cancel, "dialog_cancel");
        cancel.Click += (_, _) => DialogResult = false;

        var buttons = new StackPanel
        {
            Orientation = Orientation.Horizontal,
            HorizontalAlignment = HorizontalAlignment.Right,
            Margin = new Thickness(16),
        };
        buttons.Children.Add(cancel);
        buttons.Children.Add(confirm);

        var grid = new Grid();
        grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        Grid.SetRow(message, 0);
        Grid.SetRow(buttons, 1);
        grid.Children.Add(message);
        grid.Children.Add(buttons);
        Content = grid;
    }
}
