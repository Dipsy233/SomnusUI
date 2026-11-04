namespace SendGate;

public partial class MainWindow : System.Windows.Window
{
    public MainWindow()
    {
        InitializeComponent();
    }

    private void OnComposerChanged(object sender, System.Windows.Controls.TextChangedEventArgs e)
    {
        Send.IsEnabled = !string.IsNullOrWhiteSpace(Composer.Text);
    }
}
