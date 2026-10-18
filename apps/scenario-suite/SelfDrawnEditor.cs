using System;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Automation.Peers;
using System.Windows.Automation.Provider;
using System.Windows.Automation.Text;
using System.Windows.Input;
using System.Windows.Media;

namespace ScenarioSuite;

/// <summary>
/// A genuinely self-drawn rich-text composer, modelled on the control the
/// project's capability report measured on QQ (Electron + Chromium +
/// ProseMirror).
///
/// It is drawn by hand in <see cref="OnRender"/> and the text is kept in a
/// field, so the platform sees a *group of pixels*, not an edit control:
///
/// * no UIA **ValuePattern** - there is no setter to call;
/// * **no element HWND** - so no ``WM_SETTEXT`` / ``EM_REPLACESEL`` either;
/// * UIA reports it as a **Group** with a self-drawn class name, not as an
///   ``Edit``/``Document``;
/// * the text is readable through the UIA **Text** pattern only, exactly like
///   the measured control.
///
/// The two routes that remain are the ones the application itself owns:
/// ``WM_PASTE`` (the clipboard) and keyboard input. Both need the foreground,
/// which is why the runtime gates them behind ``--allow-clipboard`` /
/// ``--allow-keyboard`` and refuses by default.
///
/// This is deliberately *not* a WPF ``RichTextBox``. A ``RichTextBox`` is a
/// platform control that merely happens not to publish a writable Value
/// pattern; a self-drawn surface publishes nothing at all, which is the harder
/// and more common case in the wild (QQ, 豆包, Steam, VS Code, terminals).
/// </summary>
public class SelfDrawnEditor : FrameworkElement
{
    private string _text = "";

    /// <summary>Raised when the content changes, so the host can redraw.</summary>
    public event EventHandler? TextChanged;

    /// <summary>
    /// The composer's content. Exposed as a normal CLR property (not a
    /// dependency property): UIA must not find a writable property here, since
    /// there is no publishable ValuePattern to back it.
    /// </summary>
    public string Text
    {
        get => _text;
        set
        {
            if (_text == value)
            {
                return;
            }
            _text = value ?? "";
            InvalidateVisual();
            TextChanged?.Invoke(this, EventArgs.Empty);
        }
    }

    public SelfDrawnEditor()
    {
        // A self-drawn control is focusable and handles its own input; it is
        // emphatically not a text box the platform can drive.
        Focusable = true;
        FocusVisualStyle = null;
        SnapsToDevicePixels = true;
        // Accept keyboard input and the paste command, nothing else.
        CommandBindings.Add(
            new CommandBinding(
                ApplicationCommands.Paste,
                (_, _) => PasteFromClipboard(),
                (_, e) => e.CanExecute = Clipboard.ContainsText()
            )
        );
    }

    /// <summary>
    /// Publish a self-drawn automation peer so UIA sees a Group with this
    /// control's own class name, exactly as the measured QQ editor does
    /// (``Group`` + ``ProseMirror ExEditor-qq-msg-editor``).
    /// </summary>
    protected override AutomationPeer OnCreateAutomationPeer() =>
        new SelfDrawnEditorPeer(this);

    protected override void OnTextInput(TextCompositionEventArgs e)
    {
        base.OnTextInput(e);
        Text += e.Text;
        e.Handled = true;
    }

    protected override void OnKeyDown(System.Windows.Input.KeyEventArgs e)
    {
        base.OnKeyDown(e);
        switch (e.Key)
        {
            case Key.Back when _text.Length > 0:
                Text = _text[..^1];
                e.Handled = true;
                break;
            case Key.Enter:
                e.Handled = true;
                break;
        }
    }

    /// <summary>
    /// Read the clipboard the way a browser-based editor does: the application
    /// itself performs the paste. This is the route ``paste_text`` drives
    /// (Ctrl+V), and it is why that route works on a control that publishes
    /// nothing - the text never has to go through a native write interface.
    /// </summary>
    private void PasteFromClipboard()
    {
        if (Clipboard.ContainsText())
        {
            Text += Clipboard.GetText();
        }
    }

    protected override void OnRender(DrawingContext dc)
    {
        var rect = new Rect(RenderSize);
        dc.DrawRectangle(Brushes.White, new Pen(Brushes.Gray, 1), rect);
        var formatted = new FormattedText(
            _text,
            System.Globalization.CultureInfo.CurrentCulture,
            FlowDirection.LeftToRight,
            new Typeface("Segoe UI"),
            14,
            Brushes.Black,
            VisualTreeHelper.GetDpi(this).PixelsPerDip
        );
        dc.DrawText(formatted, new System.Windows.Point(6, 6));
    }
}

/// <summary>
/// The automation peer of <see cref="SelfDrawnEditor"/>.
///
/// It reports a **Group** with a self-drawn class name and no value pattern -
/// the shape the capability report measured on QQ's ProseMirror box. The point
/// of overriding this is that it is the *application* choosing what to publish;
/// a control that publishes nothing writable must be indistinguishable from one
/// that has nothing writable, because that is the truth.
/// </summary>
public class SelfDrawnEditorPeer : FrameworkElementAutomationPeer, ITextProvider
{
    public SelfDrawnEditorPeer(SelfDrawnEditor owner) : base(owner) { }

    protected override string GetClassNameCore() => "ProseMirror ExEditor";

    protected override AutomationControlType GetAutomationControlTypeCore() =>
        AutomationControlType.Group;

    /// <summary>
    /// Publish the UIA **Text** pattern for the painted content.
    ///
    /// This is what a real self-drawn editor does: QQ's ProseMirror box and
    /// every Chromium/Electron editor publish their text this way (it is how a
    /// screen reader reads them). It is the *readable* surface, and it is
    /// deliberately the only one - there is still no ValuePattern and no element
    /// HWND, so the content can be read and diffed but never written through a
    /// native interface. That is the shape the capability report measured, and
    /// the shape the set_text fallback chain exists for: readable, not writable.
    ///
    /// Publishing it matters to the runtime too. Without it, a synthetic write
    /// has no way to confirm it landed and can only wait on a timer; with it,
    /// the sender reads the content back and reports delivery as a measurement.
    /// </summary>
    public override object? GetPattern(PatternInterface patternInterface)
    {
        if (patternInterface == PatternInterface.Text)
        {
            return this;
        }
        return base.GetPattern(patternInterface);
    }

    ITextRangeProvider[] ITextProvider.GetSelection() =>
        new ITextRangeProvider[] { new SelfDrawnTextRange((SelfDrawnEditor)Owner) };

    ITextRangeProvider[] ITextProvider.GetVisibleRanges() =>
        new ITextRangeProvider[] { new SelfDrawnTextRange((SelfDrawnEditor)Owner) };

    ITextRangeProvider? ITextProvider.RangeFromChild(IRawElementProviderSimple childElement) =>
        new SelfDrawnTextRange((SelfDrawnEditor)Owner);

    ITextRangeProvider? ITextProvider.RangeFromPoint(System.Windows.Point screenLocation) =>
        new SelfDrawnTextRange((SelfDrawnEditor)Owner);

    ITextRangeProvider ITextProvider.DocumentRange =>
        new SelfDrawnTextRange((SelfDrawnEditor)Owner);

    SupportedTextSelection ITextProvider.SupportedTextSelection =>
        SupportedTextSelection.Single;
}

/// <summary>
/// The one text range this editor exposes: the whole document.
///
/// Only reading is meaningful here (the runtime reads the content back to see
/// whether a paste arrived). The editing members exist because the interface
/// demands them; they report "unsupported" rather than pretending to work,
/// which keeps the control honest about being read-only through UIA.
/// </summary>
public class SelfDrawnTextRange : ITextRangeProvider
{
    private readonly SelfDrawnEditor _owner;

    public SelfDrawnTextRange(SelfDrawnEditor owner) => _owner = owner;

    public ITextProvider TextPattern => (ITextProvider)UIElementAutomationPeer.CreatePeerForElement(_owner)!;

    public string GetText(int maxLength) =>
        maxLength < 0 ? _owner.Text : _owner.Text.Substring(0, Math.Min(maxLength, _owner.Text.Length));

    public bool Compare(ITextRangeProvider range) => range is SelfDrawnTextRange;

    public int CompareEndpoints(TextPatternRangeEndpoint endpoint, ITextRangeProvider targetRange, TextPatternRangeEndpoint targetEndpoint) => 0;

    public void ExpandToEnclosingUnit(TextUnit unit) { }

    public ITextRangeProvider? FindAttribute(int attributeId, object value, bool backward) => null;

    public ITextRangeProvider? FindText(string text, bool backward, bool ignoreCase) => null;

    public object? GetAttributeValue(int attributeId) => null;

    public double[]? GetBoundingRectangles() => null;

    public IRawElementProviderSimple? GetEnclosingElement() =>
        UIElementAutomationPeer.CreatePeerForElement(_owner) as IRawElementProviderSimple;

    public IRawElementProviderSimple[] GetChildren() => System.Array.Empty<IRawElementProviderSimple>();

    public int Move(TextUnit unit, int count) => 0;

    public int MoveEndpointByUnit(TextPatternRangeEndpoint endpoint, TextUnit unit, int count) => 0;

    public void MoveEndpointByRange(TextPatternRangeEndpoint endpoint, ITextRangeProvider targetRange, TextPatternRangeEndpoint targetEndpoint) { }

    public void Select() { }

    public void AddToSelection() { }

    public void RemoveFromSelection() { }

    public void ScrollIntoView(bool alignToTop) { }

    public ITextRangeProvider Clone() => new SelfDrawnTextRange(_owner);

    public bool Equals(ITextRangeProvider range) => range is SelfDrawnTextRange r && ReferenceEquals(r._owner, _owner);
}
