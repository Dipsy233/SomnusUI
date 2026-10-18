// The RDP control's event source, and the sink that turns it into .NET events.
//
// Why events and not polling
// --------------------------
// The control exposes a single `Connected` flag, and it becomes 1 as soon as the
// RDP transport is up - which happens whether or not the far side accepted the
// credentials. A refused logon therefore reads as "connected", and a caller that
// trusts that flag reports success for a sign-in that never happened. Measured
// here: `STATE=connected` with no child session in `query session` at all.
//
// The events are what distinguish the two states:
//   OnConnected      -> the transport is up, the far side may still be at a
//                       sign-in screen (`LoggingIn`)
//   OnLoginComplete  -> a user is actually signed in (`Connected`)
//   OnLogonError     -> the sign-in was refused, with a Win32 status code
//
// Declared as a dispatch interface with explicit DISPIDs because the typelib's
// coclass cannot be reached through a normal COM event subscription here
// (the control is a scripting host). Only the members this host uses are
// declared; undispatched members answer DISP_E_MEMBERNOTFOUND, which the
// control tolerates. Dispatch is by DISPID number, not declaration order, so
// the gaps below are harmless.
//
// Adapted from ParaDesk (MIT), which is where this pattern was proven.

using System;
using System.Runtime.InteropServices;

namespace Somnus.RdpHost;

[ComImport]
[Guid("336D5562-EFA8-482E-8CB3-C5C0FC7A7DB6")]
[InterfaceType(ComInterfaceType.InterfaceIsIDispatch)]
internal interface IMsTscAxEvents
{
    [DispId(1)] void OnConnecting();
    [DispId(2)] void OnConnected();
    [DispId(3)] void OnLoginComplete();
    [DispId(4)] void OnDisconnected(int discReason);
    [DispId(10)] void OnFatalError(int errorCode);
    [DispId(11)] void OnWarning(int warningCode);
    [DispId(12)] void OnRemoteDesktopSizeChange(int width, int height);
    [DispId(13)] void OnIdleTimeoutNotification();
    [DispId(15)] bool OnConfirmClose();
    [DispId(18)] void OnAuthenticationWarningDisplayed();
    [DispId(19)] void OnAuthenticationWarningDismissed();
    [DispId(22)] void OnLogonError(int lError);
    [DispId(33)] void OnAutoReconnected();
    [DispId(34)] void OnAutoReconnecting2(
        int disconnectReason, bool networkAvailable, int attemptCount, int maxAttemptCount);
}

/// <summary>
/// Receives the control's events and reports them to the host.
///
/// Every handler is wrapped: an exception thrown out of a COM callback becomes
/// DISP_E_EXCEPTION and is swallowed by the control, which would make a fault
/// here silent. Catching and printing it keeps a failure visible.
/// </summary>
internal sealed class RdpEventSink : IMsTscAxEvents
{
    public event Action? Connecting;
    public event Action? Connected;
    public event Action? LoginComplete;
    public event Action<int>? Disconnected;
    public event Action<int>? LogonError;
    public event Action<int, int>? RemoteSizeChanged;
    public event Action<int, int, int>? Reconnecting;
    public event Action? Reconnected;

    /// <summary>Return false to stop the control closing itself.</summary>
    public Func<bool>? ConfirmClose;

    public void OnConnecting() => Safe(nameof(OnConnecting), () => Connecting?.Invoke());

    public void OnConnected() => Safe(nameof(OnConnected), () => Connected?.Invoke());

    public void OnLoginComplete() => Safe(nameof(OnLoginComplete), () => LoginComplete?.Invoke());

    public void OnDisconnected(int discReason) =>
        Safe(nameof(OnDisconnected), () => Disconnected?.Invoke(discReason));

    public void OnLogonError(int lError) =>
        Safe(nameof(OnLogonError), () => LogonError?.Invoke(lError));

    public void OnFatalError(int errorCode) =>
        Safe(nameof(OnFatalError), () => Console.Error.WriteLine($"RDP fatal error {errorCode}"));

    public void OnWarning(int warningCode) =>
        Safe(nameof(OnWarning), () => Console.Error.WriteLine($"RDP warning {warningCode}"));

    public void OnRemoteDesktopSizeChange(int width, int height) =>
        Safe(nameof(OnRemoteDesktopSizeChange), () => RemoteSizeChanged?.Invoke(width, height));

    public void OnIdleTimeoutNotification() => Safe(nameof(OnIdleTimeoutNotification), () => { });

    public void OnAuthenticationWarningDisplayed() =>
        Safe(nameof(OnAuthenticationWarningDisplayed), () => { });

    public void OnAuthenticationWarningDismissed() =>
        Safe(nameof(OnAuthenticationWarningDismissed), () => { });

    public bool OnConfirmClose()
    {
        try { return ConfirmClose?.Invoke() ?? true; }
        catch { return true; }
    }

    public void OnAutoReconnected() => Safe(nameof(OnAutoReconnected), () => Reconnected?.Invoke());

    public void OnAutoReconnecting2(
        int disconnectReason, bool networkAvailable, int attemptCount, int maxAttemptCount) =>
        Safe(nameof(OnAutoReconnecting2),
            () => Reconnecting?.Invoke(disconnectReason, attemptCount, maxAttemptCount));

    private static void Safe(string name, Action body)
    {
        try { body(); }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"event {name} threw: {ex.GetType().Name}: {ex.Message}");
        }
    }
}
