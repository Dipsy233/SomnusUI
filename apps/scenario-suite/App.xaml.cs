using System.Windows;

namespace ScenarioSuite;

/// <summary>
/// Hosts the scenario windows. Each scenario is a separate window so a test can
/// launch exactly the one it needs (and so the accessibility tree of one screen
/// is not polluted by the others).
///
/// Usage: <c>ScenarioSuite.exe [scenario]</c>
///   store    - catalog with a search box, a scrollable list and a big dataset
///   form     - form with labelled fields, validation and a submit record
///   chat     - chat with a rich-text composer, a scrollable history and a
///              visible send log (send + verify)
///   tree     - deeply nested, expandable tree (compression by expansion level)
///   console  - conversation list + message pane (simulated chat, no rich text)
///   native   - real Win32 EDIT controls (an HWND, no Value pattern): the
///              Win32-message write route, with no external program needed
///   menu     - a menu bar and a context menu: items appear only when opened
///   dialog   - a destructive action behind a modal confirmation
/// </summary>
public partial class App : Application
{
    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        var scenario = e.Args.Length > 0 ? e.Args[0].ToLowerInvariant() : "store";
        Window window = scenario switch
        {
            "form" => new FormWindow(),
            "chat" => new ChatWindow(),
            "tree" => new TreeWindow(),
            "console" => new ConsoleWindow(),
            "native" => new NativeWindow(),
            "menu" => new MenuWindow(),
            "dialog" => new DialogWindow(),
            _ => new StoreWindow(),
        };
        window.Title = "Scenario Suite - " + scenario;
        MainWindow = window;
        window.Show();
    }
}
