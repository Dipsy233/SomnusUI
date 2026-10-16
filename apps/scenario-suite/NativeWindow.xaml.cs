using System.Windows;

namespace ScenarioSuite;

/// <summary>
/// A screen built around real Win32 controls.
///
/// WPF paints its own controls, so a WPF <c>TextBox</c> reports
/// <c>CurrentNativeWindowHandle == 0</c> and is not a Win32 window. A WinForms
/// <c>TextBox</c> is a genuine Win32 <c>EDIT</c> with an HWND of its own, so
/// hosting one reproduces a native control - the shape the runtime meets in
/// real Win32 applications, and a different provider from WPF's own.
///
/// The status line names what the controls currently hold, so a write is
/// verifiable from the observation alone.
/// </summary>
public partial class NativeWindow : Window
{
    public NativeWindow()
    {
        InitializeComponent();
        // WinForms controls carry their UIA identity on the control itself
        // (Name/AccessibleName); the WPF AutomationProperties attached property
        // cannot be set on them from XAML.
        SingleLine.Name = "native_edit";
        SingleLine.AccessibleName = "单行输入";
        MultiLine.Name = "native_multiline";
        MultiLine.AccessibleName = "多行输入";
        ReadOnlyBox.Name = "native_readonly";
        ReadOnlyBox.AccessibleName = "只读输入";
        RichBox.Name = "native_rich";
        RichBox.AccessibleName = "富文本输入";
        // Keep the status line truthful: a native control changes its own text
        // when the runtime writes through it, and the record should follow
        // without a button press.
        SingleLine.TextChanged += (_, _) => UpdateStatus();
        MultiLine.TextChanged += (_, _) => UpdateStatus();
        RichBox.TextChanged += (_, _) => UpdateStatus();
        UpdateStatus();
    }

    private void OnCopyClick(object sender, RoutedEventArgs e) => UpdateStatus();

    private void OnClearClick(object sender, RoutedEventArgs e)
    {
        SingleLine.Clear();
        MultiLine.Clear();
        RichBox.Clear();
        UpdateStatus();
    }

    private void UpdateStatus() =>
        StatusText.Text =
            $"单行=\"{SingleLine.Text}\" 多行=\"{MultiLine.Text}\" 富文本=\"{RichBox.Text}\"";
}
