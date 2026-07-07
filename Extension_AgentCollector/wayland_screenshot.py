"""
Wayland-compatible screenshot capture for the agent extension.

On Wayland/GNOME, mss's *pixel grab* fails (XGetImage), but mss's monitor
*enumeration* still works and matches exactly what the collector uses to pick a
capture screen. So we:

  1. Grab the full logical desktop via the XDG desktop portal Screenshot
     interface (jeepney, pure-python D-Bus, no system packages).
  2. Crop to the selected monitor using mss's geometry (same numbers the
     collector's choose_capture_screen() uses).
  3. Resize the crop to the collector's frame size so the model's screenshot
     coordinates line up 1:1 with what the collector's WaylandInputController
     injects.

The original collector project is not modified; this module only reads the same
monitor geometry the collector reads.
"""

import base64
import io
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import unquote, urlparse

from PIL import Image


# ---------------------------------------------------------------------------
# XDG portal screenshot
# ---------------------------------------------------------------------------

def _portal_screenshot_uri(interactive: bool = False, timeout: float = 20.0) -> str:
    """Call org.freedesktop.portal.Screenshot and return the resulting file URI."""
    from jeepney import DBusAddress, new_method_call, MatchRule, message_bus
    from jeepney.io.blocking import open_dbus_connection

    portal = DBusAddress(
        object_path="/org/freedesktop/portal/desktop",
        bus_name="org.freedesktop.portal.Desktop",
        interface="org.freedesktop.portal.Screenshot",
    )

    conn = open_dbus_connection(bus="SESSION")
    try:
        unique = conn.unique_name  # e.g. ":1.42"
        token = "cua_ext_%d" % int(time.monotonic() * 1000)
        sender = unique[1:].replace(".", "_")
        request_path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"

        # Subscribe to the Response signal BEFORE the call to avoid a race.
        match = MatchRule(
            type="signal",
            interface="org.freedesktop.portal.Request",
            member="Response",
            path=request_path,
        )
        conn.send_and_get_reply(message_bus.AddMatch(match))

        options = {
            "handle_token": ("s", token),
            "interactive": ("b", interactive),
        }
        call = new_method_call(portal, "Screenshot", "sa{sv}", ("", options))
        conn.send_and_get_reply(call)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = max(0.1, deadline - time.monotonic())
            msg = conn.receive(timeout=remaining)
            if msg is None:
                continue
            hdr = msg.header
            if hdr.fields.get(3) == "Response" and hdr.fields.get(1) == request_path:
                response_code, results = msg.body
                if response_code != 0:
                    raise RuntimeError(
                        f"Portal Screenshot denied/cancelled (code {response_code}). "
                        "Grant screenshot permission when prompted."
                    )
                uri = results.get("uri")
                if not uri:
                    raise RuntimeError("Portal returned no screenshot URI")
                # jeepney returns variants as (signature, value) tuples.
                if isinstance(uri, tuple) and len(uri) == 2:
                    uri = uri[1]
                return uri
        raise TimeoutError("Timed out waiting for portal screenshot response.")
    finally:
        conn.close()


def _load_uri_image(uri: str) -> Image.Image:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise RuntimeError(f"Unexpected screenshot URI scheme: {uri}")
    path = Path(unquote(parsed.path))
    with Image.open(path) as im:
        img = im.convert("RGB")
    try:
        path.unlink()  # portal writes a temp file we own
    except OSError:
        pass
    return img


# ---------------------------------------------------------------------------
# Monitor geometry (via mss enumeration, same as the collector)
# ---------------------------------------------------------------------------

def _monitor_rect(monitor_index: int) -> Optional[Dict[str, int]]:
    """
    Return {'left','top','width','height'} for the given 1-based monitor index.

    Preferred source: the collector's own choose_capture_screen(), so the crop
    uses the *identical* rect the collector controls (drift-proof). Falls back to
    mss enumeration if the collector module can't be imported.
    """
    rect = _rect_from_collector(monitor_index)
    if rect:
        return rect
    try:
        import mss
        with mss.mss() as sct:
            monitors = sct.monitors
    except Exception:
        return None
    if monitor_index < 1 or monitor_index >= len(monitors):
        return None
    m = monitors[monitor_index]
    return {"left": m["left"], "top": m["top"],
            "width": m["width"], "height": m["height"]}


def _rect_from_collector(monitor_index: int) -> Optional[Dict[str, int]]:
    """Import the root collector module (read-only) and reuse its screen pick."""
    import sys
    root = str(Path(__file__).resolve().parent.parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        import collector  # imports cleanly; no side effects at module load
        region = collector.choose_capture_screen(monitor_index)
    except Exception:
        return None
    if region is None:
        return None
    return {"left": region.left, "top": region.top,
            "width": region.width, "height": region.height}


def _virtual_desktop_size() -> Optional[Tuple[int, int]]:
    try:
        import mss
        with mss.mss() as sct:
            vd = sct.monitors[0]
            return vd["width"], vd["height"]
    except Exception:
        return None


def _crop_to_monitor(img: Image.Image, monitor_index: int) -> Image.Image:
    rect = _monitor_rect(monitor_index)
    if not rect:
        return img
    # Map mss virtual-desktop coords onto the portal image, scaling if the portal
    # rendered at a different resolution than mss reports (usually identical).
    vd = _virtual_desktop_size()
    sx = sy = 1.0
    if vd and vd[0] and vd[1] and (img.width != vd[0] or img.height != vd[1]):
        sx = img.width / vd[0]
        sy = img.height / vd[1]
    left = int(rect["left"] * sx)
    top = int(rect["top"] * sy)
    right = int((rect["left"] + rect["width"]) * sx)
    bottom = int((rect["top"] + rect["height"]) * sy)
    left = max(0, min(left, img.width))
    top = max(0, min(top, img.height))
    right = max(left + 1, min(right, img.width))
    bottom = max(top + 1, min(bottom, img.height))
    return img.crop((left, top, right, bottom))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def capture(monitor_index: Optional[int] = None,
            frame_size: Optional[Tuple[int, int]] = None,
            interactive: bool = False) -> Dict[str, Any]:
    """
    Capture the selected monitor and return the dict shape model_runner.py wants.

    monitor_index : 1-based screen index (defaults to CUA_CAPTURE_MONITOR env).
    frame_size    : (w, h) to resize to, matching the collector's capture frame
                    so model coords align with WaylandInputController. Defaults to
                    CUA_FRAME_WIDTH/HEIGHT env, else the collector's 1920x1080.
    """
    if monitor_index is None:
        monitor_index = int(os.environ.get("CUA_CAPTURE_MONITOR", "0"))
    if frame_size is None:
        fw = int(os.environ.get("CUA_FRAME_WIDTH", "1920"))
        fh = int(os.environ.get("CUA_FRAME_HEIGHT", "1080"))
        frame_size = (fw, fh)

    uri = _portal_screenshot_uri(interactive=interactive)
    img = _load_uri_image(uri)

    if monitor_index and monitor_index > 0:
        img = _crop_to_monitor(img, monitor_index)

    if frame_size and frame_size[0] > 0 and frame_size[1] > 0:
        if (img.width, img.height) != frame_size:
            img = img.resize(frame_size, Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return {
        "frame_id": 0,
        "timestamp": time.time(),
        "width": img.width,
        "height": img.height,
        "mime_type": "image/png",
        "image_base64": base64.b64encode(buf.getvalue()).decode("ascii"),
    }


if __name__ == "__main__":
    shot = capture()
    print(f"Captured monitor {os.environ.get('CUA_CAPTURE_MONITOR', '0')} -> "
          f"{shot['width']}x{shot['height']} PNG, "
          f"{len(shot['image_base64'])} base64 chars")
