// Ask Windows to bridge the child session's UI Automation into this process.
//
// Why this lives in the RDP host
// ------------------------------
// Window enumeration, the UIA tree and synthetic input are all confined to the
// caller's session - verified live from both sides (a session-1 process sees
// only sessions 0 and 1; a session-2 process sees only sessions 0 and 2). So no
// single process can simply "reach across".
//
// Windows does provide a supported path, and it is aimed at exactly the program
// this is: documentation for RemoteAutomationClientSession says it "is used by
// a host application (such as the remote desktop app running on the local
// machine) to plug in and implement named pipe sharing with a remote session".
// That is this process - it is the RDP client that established the child
// session - so this is where the bridge must be created.
//
// What is being tested, in order:
//   1. construct RemoteAutomationClientSession and Start() it;
//   2. does ConnectionRequested fire at all, and does it carry LocalPipeName?
//
// A null LocalPipeName, or an event that never fires, means the bridge needs
// something this process does not have (a package identity, or an RDP transport
// hook only mstsc sets up) and the design has to change. Either way the answer
// is printed, not assumed.

using System;
using Windows.UI.UIAutomation.Core;

namespace Somnus.RdpHost;

internal sealed class RdpUiaBridge : IDisposable
{
    private RemoteAutomationClientSession? _session;
    private readonly string _name;
    private readonly Guid _sessionId;

    /// <summary>Set once a connection request has been observed.</summary>
    public bool SawConnectionRequest { get; private set; }

    /// <summary>Set once the pipe name has been read off a request.</summary>
    public string? LocalPipeName { get; private set; }

    public RdpUiaBridge(string name, Guid sessionId)
    {
        _name = name;
        _sessionId = sessionId;
    }

    public void Start()
    {
        Console.Out.WriteLine("BRIDGE=constructing");
        _session = new RemoteAutomationClientSession(_name, _sessionId);

        _session.ConnectionRequested += (_, e) =>
        {
            SawConnectionRequest = true;
            try { LocalPipeName = e.LocalPipeName; } catch { LocalPipeName = null; }
            Console.Out.WriteLine("BRIDGE=connection-requested");
            Console.Out.WriteLine($"BRIDGE_PIPE={LocalPipeName ?? "<null>"}");
            try { Console.Out.WriteLine($"BRIDGE_REMOTE_PID={e.RemoteProcessId}"); }
            catch (Exception ex) { Console.Out.WriteLine($"BRIDGE_REMOTE_PID_ERROR={ex.Message}"); }
            Console.Out.Flush();
        };

        _session.Disconnected += (_, _) =>
        {
            Console.Out.WriteLine("BRIDGE=disconnected");
            Console.Out.Flush();
        };

        _session.Start();
        Console.Out.WriteLine("BRIDGE=started");
        Console.Out.Flush();
    }

    public void Dispose()
    {
        try { _session?.Stop(); }
        catch (Exception ex) { Console.Out.WriteLine($"BRIDGE_STOP_ERROR={ex.Message}"); }
        _session = null;
    }
}
