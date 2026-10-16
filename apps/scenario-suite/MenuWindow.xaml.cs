using System.Windows;
using System.Windows.Controls;

namespace ScenarioSuite;

/// <summary>
/// A screen built around a menu bar and a context menu.
///
/// The point is navigation: a menu's items are not in the accessibility tree
/// until the menu is open, so an agent has to activate the bar entry, read the
/// delta to find the item, and only then choose it. That makes this screen the
/// natural home for the menu half of the interaction model, which the other
/// screens do not exercise.
///
/// The status line names the last chosen item, so a choice is verifiable from
/// the observation alone.
/// </summary>
public partial class MenuWindow : Window
{
    public MenuWindow()
    {
        InitializeComponent();
    }

    private void OnItemClick(object sender, RoutedEventArgs e)
    {
        if (sender is MenuItem item)
        {
            StatusText.Text = $"已执行: {item.Header}";
        }
    }
}
