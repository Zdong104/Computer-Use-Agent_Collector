"""
Wayland Screenshot via PipeWire Screencast Portal.

Flow:
1. A helper GJS process sets up a ScreenCast session via XDG portal
2. User approves screen share ONCE via GNOME dialog (persisted across runs!)
3. For each screenshot, Python sends "capture <path>" to the GJS helper
4. GJS uses GStreamer to grab one frame from the PipeWire stream
5. No more dialogs needed for the entire lifetime of the session (or ever, with persist_mode=2)
"""
import os
import sys
import json
import time
import shutil
import subprocess
import threading
from pathlib import Path


# GJS script: synchronous signal-driven approach (no Promises).
# Uses persist_mode=2 so GNOME remembers the user's monitor choice.
GJS_SCREENCAST_SCRIPT = r'''
const { Gio, GLib } = imports.gi;
imports.gi.versions.Gst = '1.0';
const Gst = imports.gi.Gst;
Gst.init(null);

let loop = new GLib.MainLoop(null, false);
let bus = Gio.bus_get_sync(Gio.BusType.SESSION, null);

let portal = Gio.DBusProxy.new_for_bus_sync(
    Gio.BusType.SESSION,
    Gio.DBusProxyFlags.NONE,
    null,
    'org.freedesktop.portal.Desktop',
    '/org/freedesktop/portal/desktop',
    'org.freedesktop.portal.ScreenCast',
    null
);

let sessionHandle = null;
let pwFd = -1;
let pwNodeId = -1;
let step = 'create';
let readingCommands = false;

// ---- Portal response handler (drives the state machine) ----
bus.signal_subscribe(
    'org.freedesktop.portal.Desktop',
    'org.freedesktop.portal.Request',
    'Response',
    null,  // match any request path
    null,
    Gio.DBusSignalFlags.NONE,
    (conn, sender, path, iface, signal_name, params) => {
        let response = params.get_child_value(0).get_uint32();
        let results = params.get_child_value(1);

        if (step === 'create') {
            if (response !== 0) {
                print(JSON.stringify({error: 'CreateSession denied', code: response}));
                loop.quit();
                return;
            }
            sessionHandle = results.lookup_value('session_handle', GLib.VariantType.new('s')).get_string()[0];

            // Step 2: SelectSources (full monitor, persist permission)
            step = 'select';
            let selectArgs = {
                'handle_token': new GLib.Variant('s', 'cua_select'),
                'types': new GLib.Variant('u', 1),        // 1 = monitor
                'multiple': new GLib.Variant('b', false),
            };
            // persist_mode: 2 = persist until explicitly revoked
            // This avoids the share dialog on subsequent runs!
            try {
                selectArgs['persist_mode'] = new GLib.Variant('u', 2);
            } catch(e) { /* older portal without persist_mode */ }

            portal.call_sync('SelectSources',
                new GLib.Variant('(oa{sv})', [sessionHandle, selectArgs]),
                Gio.DBusCallFlags.NONE, 30000, null);

        } else if (step === 'select') {
            if (response !== 0) {
                print(JSON.stringify({error: 'SelectSources denied (user cancelled?)', code: response}));
                loop.quit();
                return;
            }

            // Step 3: Start the stream
            step = 'start';
            portal.call_sync('Start',
                new GLib.Variant('(osa{sv})', [sessionHandle, '', {
                    'handle_token': new GLib.Variant('s', 'cua_start'),
                }]),
                Gio.DBusCallFlags.NONE, 60000, null);

        } else if (step === 'start') {
            if (response !== 0) {
                print(JSON.stringify({error: 'Start denied', code: response}));
                loop.quit();
                return;
            }

            // Extract PipeWire node ID
            let streamsVariant = results.lookup_value('streams', null);
            if (!streamsVariant || streamsVariant.n_children() === 0) {
                print(JSON.stringify({error: 'No streams in Start response'}));
                loop.quit();
                return;
            }
            let stream = streamsVariant.get_child_value(0);
            pwNodeId = stream.get_child_value(0).get_uint32();

            // Get PipeWire fd
            try {
                let fdResult = portal.call_with_unix_fd_list_sync(
                    'OpenPipeWireRemote',
                    new GLib.Variant('(oa{sv})', [sessionHandle, {}]),
                    Gio.DBusCallFlags.NONE, 30000, null, null
                );
                let fdList = fdResult[1];
                let fdIndex = fdResult[0].get_child_value(0).get_handle();
                pwFd = fdList.get(fdIndex);
            } catch(e) {
                pwFd = -1;
            }

            print(JSON.stringify({ready: true, node_id: pwNodeId, pw_fd: pwFd}));

            // Start listening for capture commands
            startCommandLoop();
        }
    }
);

// ---- Capture a single frame via GStreamer ----
function captureFrame(outputPath) {
    try {
        let pipelineStr;
        if (pwFd >= 0) {
            pipelineStr = `pipewiresrc fd=${pwFd} path=${pwNodeId} num-buffers=1 do-timestamp=true keepalive-time=1000 always-copy=true ! videoconvert ! pngenc ! filesink location=${outputPath}`;
        } else {
            pipelineStr = `pipewiresrc path=${pwNodeId} num-buffers=1 do-timestamp=true keepalive-time=1000 always-copy=true ! videoconvert ! pngenc ! filesink location=${outputPath}`;
        }

        let pipeline = Gst.parse_launch(pipelineStr);
        pipeline.set_state(Gst.State.PLAYING);

        let gstBus = pipeline.get_bus();
        let msg = gstBus.timed_pop_filtered(10 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR);

        if (msg !== null && msg.type === Gst.MessageType.ERROR) {
            let [err, debug] = msg.parse_error();
            pipeline.set_state(Gst.State.NULL);
            return JSON.stringify({error: `GStreamer error: ${err.message}`});
        }

        pipeline.set_state(Gst.State.NULL);

        let file = Gio.File.new_for_path(outputPath);
        if (file.query_exists(null)) {
            return JSON.stringify({success: true, path: outputPath});
        } else {
            return JSON.stringify({error: 'Output file not created'});
        }
    } catch(e) {
        return JSON.stringify({error: e.message});
    }
}

// ---- Stdin command loop ----
function startCommandLoop() {
    if (readingCommands) return;
    readingCommands = true;

    let stdin = Gio.DataInputStream.new(
        new Gio.UnixInputStream({fd: 0, close_fd: false})
    );

    function readNext() {
        stdin.read_line_async(GLib.PRIORITY_DEFAULT, null, (source, res) => {
            try {
                let [line] = source.read_line_finish_utf8(res);
                if (line === null) {
                    loop.quit();
                    return;
                }
                line = line.trim();
                if (line === 'quit') {
                    loop.quit();
                    return;
                }
                if (line.startsWith('capture ')) {
                    let path = line.substring(8).trim();
                    let result = captureFrame(path);
                    print(result);
                }
                readNext();
            } catch(e) {
                print(JSON.stringify({error: `stdin error: ${e.message}`}));
                loop.quit();
            }
        });
    }
    readNext();
}

// ---- Kick off: CreateSession ----
let sessionResult = portal.call_sync('CreateSession',
    new GLib.Variant('(a{sv})', [{
        'session_handle_token': new GLib.Variant('s', 'cua_session'),
        'handle_token': new GLib.Variant('s', 'cua_create'),
    }]),
    Gio.DBusCallFlags.NONE, 30000, null
);

// Safety timeout
GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 120, () => {
    if (!readingCommands) {
        print(JSON.stringify({error: 'Setup timeout (120s) - did you approve the share dialog?'}));
        loop.quit();
    }
    return false;
});

loop.run();
'''


class PipeWireScreenshotter:
    """Take screenshots via PipeWire screencast on Wayland GNOME.
    
    One-time setup: user clicks 'Share' in the GNOME dialog.
    persist_mode=2 means GNOME will remember the choice; subsequent
    runs may skip the dialog entirely.
    """

    def __init__(self):
        self._session_proc = None
        self._node_id = None
        self._ready = False
        self._lock = threading.Lock()

    def start_session(self) -> bool:
        """Start a PipeWire screencast session. Shows a one-time share dialog."""
        print("  🖥️  Requesting screen share (one-time approval)...")
        print("  📢 A GNOME dialog will appear — click 'Share' on your monitor.")
        print("     (This is remembered for future runs!)")

        script_path = '/tmp/cua_screencast.js'
        with open(script_path, 'w') as f:
            f.write(GJS_SCREENCAST_SCRIPT)

        self._session_proc = subprocess.Popen(
            ['gjs', script_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        try:
            line = self._session_proc.stdout.readline()
            if not line:
                err = self._session_proc.stderr.read()
                print(f"  ❌ Screencast session failed: {err[:500]}")
                return False

            data = json.loads(line.strip())

            if 'error' in data:
                print(f"  ❌ Screencast error: {data['error']}")
                return False

            if data.get('ready'):
                self._node_id = data['node_id']
                self._ready = True
                print(f"  ✅ Screen share active! PipeWire node: {self._node_id}")
                print(f"     Full-screen capture ready — no further dialogs needed.")
                return True

            print(f"  ❌ Unexpected response: {data}")
            return False

        except (json.JSONDecodeError, KeyError, IndexError) as e:
            print(f"  ❌ Failed to parse screencast response: {e}")
            try:
                stderr = self._session_proc.stderr.read()
                if stderr:
                    print(f"  stderr: {stderr[:500]}")
            except Exception:
                pass
            return False

    def capture(self, output_path: str) -> bool:
        """Capture a single full-screen frame from the PipeWire stream."""
        if not self._ready or not self._session_proc:
            print("  ❌ PipeWire session not ready!")
            return False

        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        with self._lock:
            try:
                self._session_proc.stdin.write(f'capture {output_path}\n')
                self._session_proc.stdin.flush()

                line = self._session_proc.stdout.readline()
                if not line:
                    print("  ❌ GJS helper stopped responding")
                    self._ready = False
                    return False

                result = json.loads(line.strip())
                if result.get('success'):
                    return True
                else:
                    print(f"  ❌ Capture failed: {result.get('error', 'unknown')}")
                    return False

            except Exception as e:
                print(f"  ❌ Capture error: {e}")
                return False

    def stop(self):
        """Stop the screencast session."""
        if self._session_proc:
            try:
                self._session_proc.stdin.write('quit\n')
                self._session_proc.stdin.flush()
                self._session_proc.wait(timeout=3)
            except Exception:
                try:
                    self._session_proc.terminate()
                    self._session_proc.wait(timeout=2)
                except Exception:
                    try:
                        self._session_proc.kill()
                    except Exception:
                        pass
        self._ready = False


class NonInteractivePortalScreenshotter:
    """Fallback: use XDG Screenshot portal with interactive=false.
    
    On GNOME 45+, this silently captures the full screen.
    On older GNOME, it may be denied (response=2).
    """

    def capture(self, output_path: str) -> bool:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        gjs_cmd = '''
const { Gio, GLib } = imports.gi;
let loop = new GLib.MainLoop(null, false);
let bus = Gio.bus_get_sync(Gio.BusType.SESSION, null);
let portal = Gio.DBusProxy.new_for_bus_sync(
    Gio.BusType.SESSION, Gio.DBusProxyFlags.NONE, null,
    'org.freedesktop.portal.Desktop',
    '/org/freedesktop/portal/desktop',
    'org.freedesktop.portal.Screenshot', null
);
let result = portal.call_sync('Screenshot',
    new GLib.Variant('(sa{sv})', ['', {'interactive': new GLib.Variant('b', false)}]),
    Gio.DBusCallFlags.NONE, 30000, null);
let requestPath = result.get_child_value(0).get_string()[0];
bus.signal_subscribe('org.freedesktop.portal.Desktop',
    'org.freedesktop.portal.Request', 'Response', requestPath, null,
    Gio.DBusSignalFlags.NONE,
    (conn, sender, path, iface, signal, params) => {
        let response = params.get_child_value(0).get_uint32();
        if (response === 0) {
            let uri = params.get_child_value(1).lookup_value('uri', GLib.VariantType.new('s'));
            if (uri) {
                let file = Gio.File.new_for_uri(uri.get_string()[0]);
                print(file.get_path());
            }
        } else {
            printerr('portal response: ' + response);
        }
        loop.quit();
    }
);
GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 10, () => { loop.quit(); return false; });
loop.run();
'''
        try:
            result = subprocess.run(
                ['gjs', '-c', gjs_cmd],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0 and result.stdout.strip():
                src = result.stdout.strip()
                if os.path.isfile(src):
                    shutil.copy2(src, output_path)
                    return True
        except Exception as e:
            print(f"  ❌ Non-interactive portal screenshot failed: {e}")
        return False

    def stop(self):
        pass


class GnomeShellScreenshotter:
    """Fallback: use org.gnome.Shell.Screenshot D-Bus (GNOME < 43 era)."""

    def capture(self, output_path: str) -> bool:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        try:
            r = subprocess.run([
                'gdbus', 'call', '--session',
                '--dest', 'org.gnome.Shell.Screenshot',
                '--object-path', '/org/gnome/Shell/Screenshot',
                '--method', 'org.gnome.Shell.Screenshot.Screenshot',
                'true', 'false', output_path,
            ], capture_output=True, text=True, timeout=10)
            if r.returncode == 0 and 'true' in r.stdout:
                return os.path.isfile(output_path)
        except Exception:
            pass
        return False

    def stop(self):
        pass


def create_wayland_screenshotter():
    """Create the best available Wayland screenshotter.
    
    Tries in order:
    1. PipeWire ScreenCast (best: zero-overhead repeated captures after one-time approval)
    2. Non-interactive XDG Screenshot portal (works on some GNOME versions)
    3. org.gnome.Shell.Screenshot D-Bus (legacy GNOME < 43)
    """
    # 1. PipeWire ScreenCast — gold standard
    pw = PipeWireScreenshotter()
    if pw.start_session():
        return pw

    # 2. Non-interactive portal
    print("  ⚠️  PipeWire screencast failed. Trying non-interactive portal…")
    ni = NonInteractivePortalScreenshotter()
    if ni.capture('/tmp/cua_portal_test.png'):
        print("  ✅ Non-interactive portal works!")
        return ni

    # 3. Legacy GNOME Shell D-Bus
    print("  ⚠️  Portal failed. Trying GNOME Shell D-Bus…")
    gs = GnomeShellScreenshotter()
    if gs.capture('/tmp/cua_dbus_test.png'):
        print("  ✅ GNOME Shell D-Bus screenshot works!")
        return gs

    print("  ❌ No silent screenshot method available.")
    print("     Install gnome-screenshot: sudo apt install gnome-screenshot")
    return None
