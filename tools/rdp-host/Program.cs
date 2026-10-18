using System;
using System.Runtime.InteropServices;
using System.Windows.Forms;

namespace Somnus.RdpHost;

/// <summary>
/// Splits a saved account name into the control's separate Domain/UserName
/// fields, keeping online-provider prefixes intact.
///
/// ``MACHINE\user`` becomes Domain=MACHINE, User=user. A provider-qualified
/// name (``MicrosoftAccount\...``, ``AzureAD\...``) is *not* a Windows domain -
/// it tells the logon which identity provider to use - so it stays whole in the
/// user field with an empty domain. Passing it as a domain instead makes the
/// sign-in fail with a misleading "wrong password". Same rule as ParaDesk's
/// RdpCredentialName.Parse, for the same reason.
/// </summary>
internal readonly struct RdpCredentialName
{
    public string Domain { get; init; }
    public string User { get; init; }

    public static RdpCredentialName Parse(string name)
    {
        string value = name ?? "";
        int separator = value.IndexOf('\\');
        if (separator <= 0)
            return new RdpCredentialName { Domain = "", User = value };

        string prefix = value[..separator];
        bool provider = prefix.Equals("MicrosoftAccount", StringComparison.OrdinalIgnoreCase)
            || prefix.Equals("AzureAD", StringComparison.OrdinalIgnoreCase);
        if (provider)
            return new RdpCredentialName { Domain = "", User = value };

        return new RdpCredentialName
        {
            Domain = prefix,
            User = value[(separator + 1)..],
        };
    }
}

/// <summary>
/// IMsRdpExtendedSettings - the property bag that carries
/// "ConnectToChildSession". It derives from IUnknown, not IDispatch, so it is
/// invisible to late binding and must be reached through a ComImport cast.
/// </summary>
[ComImport]
[Guid("302D8188-0052-4807-806A-362B628F9AC5")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IMsRdpExtendedSettings
{
    void set_Property(
        [In, MarshalAs(UnmanagedType.BStr)] string propertyName,
        [In, MarshalAs(UnmanagedType.Struct)] ref object value);

    [return: MarshalAs(UnmanagedType.Struct)]
    object get_Property([In, MarshalAs(UnmanagedType.BStr)] string propertyName);
}

/// <summary>
/// The RDP ActiveX control, hosted by AxHost.
///
/// CLSID is <c>MsRdpClient10NotSafeForScripting</c>: the scripting-safe variant
/// hides IMsRdpExtendedSettings, so it cannot ask for a child session at all.
///
/// The event sink is attached through <see cref="CreateSink"/>: AxHost calls it
/// after the interfaces are attached but before the control is usable, which is
/// the only window in which a connection point can be advised. A sink attached
/// later misses the logon events that happen during the initial connection -
/// the exact ones this host needs.
/// </summary>
internal sealed class RdpControl : AxHost
{
    private AxHost.ConnectionPointCookie? _cookie;

    /// <summary>Event receiver. Must be set before the control is created.</summary>
    public RdpEventSink? Sink { get; set; }

    public RdpControl() : base("a0c63c30-f08d-4ab4-907c-34905d770c7d") { }

    public object Ocx => GetOcx();

    protected override void CreateSink()
    {
        base.CreateSink();
        if (Sink is null) return;
        try
        {
            _cookie = new AxHost.ConnectionPointCookie(
                GetOcx(), Sink, typeof(IMsTscAxEvents));
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"could not subscribe to RDP events: {ex.Message}");
            _cookie = null;
        }
    }

    protected override void DetachSink()
    {
        try
        {
            _cookie?.Disconnect();
        }
        catch
        {
            // Detaching during teardown: nothing useful to do about a failure.
        }
        _cookie = null;
        base.DetachSink();
    }
}

/// <summary>
/// The container window. It exists to give the control a site and a parent so
/// the control's HWND can be created; the session itself is created by the
/// control, not by this window.
/// </summary>
internal sealed class HostForm : Form
{
    private readonly RdpControl _control;
    private readonly RdpEventSink _sink = new();
    private readonly string _server;
    private readonly bool _redirectClipboard;
    private readonly string _user;
    private readonly string _password;
    private readonly bool _startBridge;
    private bool _bridgeAttempted;
    private readonly string _bridgeName;
    private readonly Guid _bridgeId;
    private RdpUiaBridge? _bridge;

    public bool EverConnected { get; private set; }

    public HostForm(
        string server,
        bool redirectClipboard,
        int width,
        int height,
        string user,
        string password,
        bool startBridge,
        string bridgeName,
        Guid bridgeId)
    {
        _server = server;
        _redirectClipboard = redirectClipboard;
        _user = user;
        _password = password;
        _startBridge = startBridge;
        _bridgeName = bridgeName;
        _bridgeId = bridgeId;

        Text = "SomnusUI - child session (RDP loopback)";
        ClientSize = new System.Drawing.Size(width, height);
        StartPosition = FormStartPosition.CenterScreen;

        _control = new RdpControl
        {
            Dock = DockStyle.Fill,
            // Set before the control is created: AxHost only advises the
            // connection point during CreateSink, so a sink assigned later
            // would miss the logon events of the initial connection.
            Sink = _sink,
        };
        Controls.Add(_control);

        _sink.Connected += () => Report("logging-in");
        _sink.LoginComplete += () =>
        {
            Report("logged-on");
            // The bridge is created only once a user is signed in: it pairs with
            // a live remote session, so starting it before there is one (or
            // while the far side is still at a sign-in screen) has nothing to
            // pair with.
            StartBridgeIfRequested();
        };
        _sink.LogonError += code =>
        {
            string desc = LogonErrorText(code);
            Report(desc is null ? "logging-in" : "logon-failed");
            Console.Out.WriteLine($"LOGON_ERROR={code} ({desc ?? "no description"})");
            Console.Out.Flush();
        };
        _sink.Disconnected += reason => Report($"disconnected ({reason})");
    }

    private string _lastReport = "";

    private void StartBridgeIfRequested()
    {
        if (!_startBridge || _bridgeAttempted) return;
        _bridgeAttempted = true;
        try
        {
            _bridge = new RdpUiaBridge(_bridgeName, _bridgeId);
            _bridge.Start();
        }
        catch (Exception ex)
        {
            Console.Out.WriteLine($"BRIDGE_START_FAILED={ex.GetType().Name}: {ex.Message}");
            Console.Out.Flush();
            _bridge = null;
        }
    }

    private void Report(string state)
    {
        if (state == _lastReport) return;
        _lastReport = state;
        Console.Out.WriteLine($"STATE={state}");
        Console.Out.Flush();
    }

    /// <summary>
    /// The control's logon status codes, in words.
    ///
    /// The control reports a logon failure as a Win32/NTSTATUS value. The values
    /// that matter here are the ones that distinguish "the credentials were
    /// refused" from "the account cannot be used for this logon", because the
    /// user's next step differs. Same mapping ParaDesk uses, same reasons.
    /// </summary>
    private static string? LogonErrorText(int error)
    {
        const int StatusLogonFailure = unchecked((int)0xC000006D);
        const int StatusAccountRestriction = unchecked((int)0xC000006E);
        const int StatusPasswordMustChange = unchecked((int)0xC0000224);
        return error switch
        {
            -2 or 3 => null,  // "in progress" notifications, not failures
            0 or StatusLogonFailure => "the user name or password is incorrect",
            1 => "the account password has expired",
            2 => "the sign-in did not complete (stopped at the logon screen)",
            StatusAccountRestriction =>
                "the account is restricted (logon hours, or blank password not allowed)",
            StatusPasswordMustChange => "the account must change its password first",
            _ => error is < 0 and >= -7
                ? "the sign-in was interrupted by session arbitration"
                : "logon failed",
        };
    }

    protected override void OnShown(EventArgs e)
    {
        base.OnShown(e);
        try
        {
            Configure();
            _control.Ocx.GetType().InvokeMember(
                "Connect",
                System.Reflection.BindingFlags.InvokeMethod,
                null,
                _control.Ocx,
                null);
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"error: {ex.Message}");
            Console.Out.WriteLine("STATE=failed");
            Console.Out.Flush();
        }
    }

    private void Configure()
    {
        dynamic ocx = _control.Ocx;

        ocx.Server = _server;
        ocx.DesktopWidth = ClientSize.Width;
        ocx.DesktopHeight = ClientSize.Height;
        ocx.ColorDepth = 32;

        object adv = GetAdvanced(ocx);
        if (adv is not null)
        {
            dynamic a = adv;
            TrySet(() => a.EnableCredSspSupport = true);
            TrySet(() => a.AuthenticationLevel = 0);
            // The container owns full screen, so the control does not spawn a
            // second top-level window that fights this one for the z-order.
            TrySet(() => a.ContainerHandledFullScreen = 1);
            TrySet(() => a.DisplayConnectionBar = false);
            TrySet(() => a.RedirectClipboard = _redirectClipboard);
            TrySet(() => a.SmartSizing = true);
            TrySet(() => a.EnableAutoReconnect = true);
            // Never grab the foreground when the second desktop appears.
            TrySet(() => a.GrabFocusOnConnect = false);
            TrySet(() => a.allowBackgroundInput = 0);
        }

        // The one property that turns a loopback into a *child* session.
        var ext = (IMsRdpExtendedSettings)_control.Ocx;
        object on = true;
        ext.set_Property("ConnectToChildSession", ref on);

        Console.Out.WriteLine($"CHILDSESSION_PROPERTY={ext.get_Property("ConnectToChildSession")}");

        // Credentials, when the caller supplied them, let the loopback sign in
        // without a person at the window. Without them the control stops at the
        // sign-in screen - which is a supported mode, not a failure.
        //
        // The password arrives on the command line because that is the only
        // channel this host has. It is therefore visible to anything that can
        // read this process's arguments while it runs, and the caller is
        // expected to have taken that into account (see
        // somnus/session/credentials.py, which stores the secret encrypted and
        // hands it over only to this short-lived child process).
        if (!string.IsNullOrEmpty(_user))
        {
            var name = RdpCredentialName.Parse(_user);
            TrySet(() => ocx.UserName = name.User);
            if (!string.IsNullOrEmpty(name.Domain))
                TrySet(() => ocx.Domain = name.Domain);
            Console.Out.WriteLine($"CREDENTIAL_USER={name.User}");
            if (!string.IsNullOrEmpty(name.Domain))
                Console.Out.WriteLine($"CREDENTIAL_DOMAIN={name.Domain}");
        }

        if (!string.IsNullOrEmpty(_password) && adv is not null)
        {
            dynamic a = adv;
            string secret = _password;
            if (TrySet(() => a.ClearTextPassword = secret))
                Console.Out.WriteLine("CREDENTIAL_PASSWORD=set");
            else
                Console.Out.WriteLine("CREDENTIAL_PASSWORD=failed");
        }

        Console.Out.Flush();
    }

    private static object? GetAdvanced(dynamic ocx)
    {
        foreach (string name in new[]
                 {
                     "AdvancedSettings9", "AdvancedSettings8", "AdvancedSettings7",
                     "AdvancedSettings2",
                 })
        {
            try
            {
                object value = ocx.GetType().InvokeMember(
                    name, System.Reflection.BindingFlags.GetProperty, null, ocx, null)!;
                if (value is not null) return value;
            }
            catch
            {
                // Try the next, older settings interface.
            }
        }
        return null;
    }

    private static bool TrySet(Action setter)
    {
        try
        {
            setter();
            return true;
        }
        catch
        {
            // An optional property on this build; not fatal.
            return false;
        }
    }
    /// <summary>Poll the connection state and report transitions on stdout.</summary>
    public void Watch()
    {
        // Nothing to poll. The state is driven by the control's own events
        // (see the constructor), which is the only way to tell a completed
        // sign-in from a transport that is merely up. Polling `Connected` here
        // was what reported a refused logon as success.
    }
}

internal static class Program
{
    [STAThread]
    private static int Main(string[] args)
    {
        var options = Options.Parse(args);
        if (options is null)
        {
            Console.Error.WriteLine(
                "usage: somnus-rdp-host [--server localhost] [--no-clipboard] "
                + "[--width N] [--height N] [--headless] "
                + "[--user NAME] [--password SECRET] "
                + "[--bridge] [--bridge-name NAME] [--bridge-id GUID]");
            return 64;
        }

        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);

        var form = new HostForm(
            options.Server,
            options.RedirectClipboard,
            options.Width,
            options.Height,
            options.User,
            options.Password,
            options.Bridge,
            options.BridgeName,
            options.BridgeId);
        form.Watch();

        if (options.Headless)
        {
            // Still a real window - the control needs one - but parked off the
            // visible desktop so a test run does not disturb the tester.
            form.StartPosition = FormStartPosition.Manual;
            form.Location = new System.Drawing.Point(-4000, -4000);
            form.ShowInTaskbar = false;
        }
        Console.Out.WriteLine("READY=1");
        Console.Out.Flush();

        Application.Run(form);
        return 0;
    }

}

internal sealed class Options
{
    public string Server = "localhost";
    public bool RedirectClipboard = true;
    public int Width = 1280;
    public int Height = 800;
    public bool Headless;
    public string User = "";
    public string Password = "";
    public bool Bridge;
    public string BridgeName = "somnus";
    public Guid BridgeId = new("6f3a1c2e-8b4d-4e6f-9a01-2c3d4e5f6071");

    public static Options? Parse(string[] args)
    {
        var o = new Options();
        for (int i = 0; i < args.Length; i++)
        {
            switch (args[i])
            {
                case "--server" when i + 1 < args.Length:
                    o.Server = args[++i];
                    break;
                case "--no-clipboard":
                    o.RedirectClipboard = false;
                    break;
                case "--clipboard":
                    o.RedirectClipboard = true;
                    break;
                case "--width" when i + 1 < args.Length && int.TryParse(args[++i], out var w):
                    o.Width = w;
                    break;
                case "--height" when i + 1 < args.Length && int.TryParse(args[++i], out var h):
                    o.Height = h;
                    break;
                case "--headless":
                    o.Headless = true;
                    break;
                case "--user" when i + 1 < args.Length:
                    o.User = args[++i];
                    break;
                case "--password" when i + 1 < args.Length:
                    o.Password = args[++i];
                    break;
                case "--bridge":
                    o.Bridge = true;
                    break;
                case "--bridge-name" when i + 1 < args.Length:
                    o.BridgeName = args[++i];
                    break;
                case "--bridge-id" when i + 1 < args.Length && Guid.TryParse(args[++i], out var g):
                    o.BridgeId = g;
                    break;
                case "--help":
                case "-h":
                    return null;
                default:
                    Console.Error.WriteLine($"unknown argument: {args[i]}");
                    return null;
            }
        }
        return o;
    }
}

