using System.Windows;
using System.Windows.Controls;

namespace ScenarioSuite;

/// <summary>
/// A form: labelled fields (one writable, one read-only), a notes field, a
/// checkbox, a combo box, a disabled button, a gated submit and a visible
/// record of every submission. Covers the "form + validation + verify" case:
/// the agent writes fields, submits, and reads the record back from the same
/// observation.
/// </summary>
public partial class FormWindow : Window
{
    private int _sequence = 1;

    public FormWindow()
    {
        InitializeComponent();
        Records.Items.Add("（尚无提交）");
    }

    private void OnSubmitClick(object sender, RoutedEventArgs e)
    {
        if (string.IsNullOrWhiteSpace(NameBox.Text))
        {
            ValidationText.Text = "姓名不能为空";
            return;
        }
        if (!AgreeBox.IsChecked.GetValueOrDefault())
        {
            ValidationText.Text = "必须同意条款";
            return;
        }
        ValidationText.Text = "";
        var priority = (PriorityBox.SelectedItem as ComboBoxItem)?.Content?.ToString() ?? "低";
        // The record is the verification surface: it is a normal list item, so
        // a test can read it from the observation instead of reaching into the
        // process.
        Records.Items.Add(
            $"#{_sequence++} {NameBox.Text} <{EmailBox.Text}> 优先级={priority}");
        IdBox.Text = $"REQ-{_sequence:D4}";
    }

    private void OnResetClick(object sender, RoutedEventArgs e)
    {
        NameBox.Text = "";
        EmailBox.Text = "";
        NotesBox.Text = "";
        // Restore the neutral state rather than an empty string: the line is
        // always present, so "no error" is a readable value and not a missing
        // node. (An empty TextBlock is an anonymous leaf the observation folds.)
        ValidationText.Text = "未提交";
        AgreeBox.IsChecked = false;
        PriorityBox.SelectedIndex = 0;
    }
}

