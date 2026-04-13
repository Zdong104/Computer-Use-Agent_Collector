import sys
import os
import json
import socket
import time

# Conda and virtualenvs often hijack sys.path even if we run /usr/bin/python3
# Force the system dist-packages where PyGObject ('gi') actually lives:
sys.path.append('/usr/lib/python3/dist-packages')
import gi
from gi.repository import Gio, GLib

def main():
    if len(sys.argv) < 2:
        print("Usage: dbus_helper.py <socket_fd>")
        sys.exit(1)
        
    sock_fd = int(sys.argv[1])
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM, fileno=sock_fd)
    except Exception as e:
        print(f"Failed to wrap socket: {e}", file=sys.stderr)
        sys.exit(1)

    portal = Gio.DBusProxy.new_for_bus_sync(
        Gio.BusType.SESSION, Gio.DBusProxyFlags.NONE, None,
        'org.freedesktop.portal.Desktop',
        '/org/freedesktop/portal/desktop',
        'org.freedesktop.portal.ScreenCast', None
    )

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    pwFd = -1
    pwNodeId = -1
    sessionHandle = None
    step = 'create'
    ready = False

    loop = GLib.MainLoop()

    def on_portal_response(conn, sender, path, iface, signal_name, params):
        nonlocal step, sessionHandle, pwFd, pwNodeId, ready
        response = params.get_child_value(0).get_uint32()
        results = params.get_child_value(1)

        if step == 'create':
            if response != 0:
                sock.sendall(json.dumps({"error": "CreateSession denied"}).encode())
                loop.quit()
                return

            sessionHandle = results.lookup_value('session_handle', GLib.VariantType('s')).get_string()
            step = 'select'

            selectArgs = {
                'handle_token': GLib.Variant('s', 'cua_select_v2'),
                'types': GLib.Variant('u', 1), # monitor
                'multiple': GLib.Variant('b', False),
                'persist_mode': GLib.Variant('u', 2), # avoid prompt
            }
            portal.call('SelectSources',
                GLib.Variant('(oa{sv})', [sessionHandle, selectArgs]),
                Gio.DBusCallFlags.NONE, 30000, None, None, None)

        elif step == 'select':
            if response != 0:
                sock.sendall(json.dumps({"error": "SelectSources denied"}).encode())
                loop.quit()
                return
                
            step = 'start'
            portal.call('Start',
                GLib.Variant('(osa{sv})', [sessionHandle, '', {
                    'handle_token': GLib.Variant('s', 'cua_start_v2')
                }]), Gio.DBusCallFlags.NONE, 60000, None, None, None)

        elif step == 'start':
            if response != 0:
                sock.sendall(json.dumps({"error": "Start denied"}).encode())
                loop.quit()
                return
                
            streamsVariant = results.lookup_value('streams', None)
            if not streamsVariant or streamsVariant.n_children() == 0:
                sock.sendall(json.dumps({"error": "No streams"}).encode())
                loop.quit()
                return
                
            stream = streamsVariant.get_child_value(0)
            pwNodeId = stream.get_child_value(0).get_uint32()

            try:
                res_variant, fd_list = portal.call_with_unix_fd_list_sync(
                    'OpenPipeWireRemote',
                    GLib.Variant('(oa{sv})', [sessionHandle, {}]),
                    Gio.DBusCallFlags.NONE, 30000, None, None
                )
                fd_index = res_variant.unpack()[0]
                pwFd = fd_list.get(fd_index)
            except Exception as e:
                sock.sendall(json.dumps({"error": f"OpenPipeWireRemote failed: {e}"}).encode())
                loop.quit()
                return

            # SEND THE FD VIA UNIX SOCKET SCM_RIGHTS!
            msg = json.dumps({"ready": True, "node_id": pwNodeId}).encode('utf-8')
            try:
                import array
                socket.send_fds(sock, [msg], [pwFd])
            except Exception as e:
                # fallback for older pythons if needed, but 3.9+ has socket.send_fds
                sock.sendall(json.dumps({"error": f"send_fds failed: {e}"}).encode())
                loop.quit()
                return

            ready = True
            print(f"Portal helper active. Node ID: {pwNodeId}, sent FD: {pwFd}", file=sys.stderr)

    bus.signal_subscribe(
        'org.freedesktop.portal.Desktop',
        'org.freedesktop.portal.Request',
        'Response', None, None, Gio.DBusSignalFlags.NONE,
        on_portal_response
    )

    # Kickoff
    portal.call('CreateSession',
        GLib.Variant('(a{sv})', [{
            'session_handle_token': GLib.Variant('s', 'cua_session_v2'),
            'handle_token': GLib.Variant('s', 'cua_create_v2'),
        }]), Gio.DBusCallFlags.NONE, 30000, None, None, None)

    # Thread to monitor the socket so we quit when parent closes
    def read_sock():
        try:
            req = sock.recv(1024)
            if not req or req.strip() == b'quit':
                loop.quit()
        except:
            loop.quit()

    import threading
    t = threading.Thread(target=read_sock, daemon=True)
    t.start()

    GLib.timeout_add_seconds(120, lambda: not ready and loop.quit())
    loop.run()

if __name__ == '__main__':
    main()
