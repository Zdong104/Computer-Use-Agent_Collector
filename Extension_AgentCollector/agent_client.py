"""
CUA Agent Client — Pilot the CUA Collector Pipeline Programmatically
=====================================================================
Allows an AI agent or automated script to execute GUI actions through the CUA Collector.

Features:
  - Automates task start / end
  - Injects actions (clicks, drags, keyboard input, scrolls) using pyautogui
  - Locks screen during actions (automatic transparent overlay prevents user conflict)
  - Seamlessly records action data and pre/post screenshots in the background
"""

import json
import urllib.request
import urllib.error
import time
from typing import Optional, Dict, Any


class AgentClient:
    """Python client for programmatically controlling the GUI and recording actions."""

    def __init__(self, base_url: str = "http://127.0.0.1:8321"):
        self.base_url = base_url.rstrip('/')

    def _post(self, path: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(
            url,
            data=json.dumps(data or {}).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        try:
            with urllib.request.urlopen(req) as res:
                return json.loads(res.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            try:
                err_data = json.loads(e.read().decode('utf-8'))
                raise RuntimeError(f"HTTP {e.code}: {err_data.get('error', e.reason)}")
            except Exception:
                raise RuntimeError(f"HTTP {e.code}: {e.reason}")
        except Exception as e:
            raise RuntimeError(f"Failed to connect to agent server at {url}: {e}")

    def _get(self, path: str) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            with urllib.request.urlopen(url) as res:
                return json.loads(res.read().decode('utf-8'))
        except Exception as e:
            raise RuntimeError(f"Failed to connect to agent server at {url}: {e}")

    def get_status(self) -> Dict[str, Any]:
        """Get the current state of the CUA Collector."""
        return self._get('/api/status')

    def start_task(self, description: str) -> Dict[str, Any]:
        """Start a new automated task session with a description."""
        return self._post('/api/task/start', {"description": description})

    def end_task(self) -> Dict[str, Any]:
        """End the current task session and finalize index files."""
        return self._post('/api/task/end')

    def click(self, x: int, y: int, button: str = "left") -> Dict[str, Any]:
        """Perform a mouse click at local screen coordinates (x, y)."""
        return self._post('/api/action', {"type": "click", "x": x, "y": y, "button": button})

    def double_click(self, x: int, y: int, button: str = "left") -> Dict[str, Any]:
        """Perform a double-click at local screen coordinates (x, y)."""
        return self._post('/api/action', {"type": "double_click", "x": x, "y": y, "button": button})

    def drag(self, press_x: int, press_y: int, release_x: int, release_y: int, button: str = "left", duration: float = 0.5) -> Dict[str, Any]:
        """Drag the mouse from (press_x, press_y) to (release_x, release_y)."""
        return self._post('/api/action', {
            "type": "drag",
            "press_x": press_x,
            "press_y": press_y,
            "release_x": release_x,
            "release_y": release_y,
            "button": button,
            "duration": duration
        })

    def write(self, text: str) -> Dict[str, Any]:
        """Type text on the keyboard."""
        return self._post('/api/action', {"type": "write", "text": text})

    def press_key(self, key: str) -> Dict[str, Any]:
        """Press a keyboard key (e.g. 'enter', 'tab', 'backspace')."""
        return self._post('/api/action', {"type": "press_key", "key": key})

    def hotkey(self, *keys: str) -> Dict[str, Any]:
        """Perform a hotkey combination (e.g. hotkey('ctrl', 't'))."""
        return self._post('/api/action', {"type": "hotkey", "keys": list(keys)})

    def scroll(self, dx: int = 0, dy: int = 0, x: Optional[int] = None, y: Optional[int] = None) -> Dict[str, Any]:
        """Scroll the mouse wheel. dy > 0 scrolls up, dy < 0 scrolls down."""
        data = {"type": "scroll", "dx": dx, "dy": dy}
        if x is not None:
            data["x"] = x
        if y is not None:
            data["y"] = y
        return self._post('/api/action', data)

    def send_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Send a raw action dict to the collector (must include a 'type').

        Lets callers use the full action vocabulary (move, right_click,
        mouse_down/up, key_down/up, ...) without a dedicated method each.
        """
        return self._post('/api/action', action)


if __name__ == '__main__':
    # Simple self-test code when run directly
    print("Testing CUA Agent Client...")
    client = AgentClient()
    
    try:
        status = client.get_status()
        print(f"Connection successful! Status: {status}")
        
        # Test start task
        print("Starting task...")
        start_res = client.start_task("AI Agent Self-Test run")
        print(f"Task started: {start_res}")
        
        # Wait a moment
        time.sleep(1)
        
        # Perform some clicks (coordinates are dummy or safe coordinates)
        print("Clicking at (500, 500)...")
        client.click(500, 500)
        
        time.sleep(1)
        
        # End task
        print("Ending task...")
        end_res = client.end_task()
        print(f"Task ended: {end_res}")
        
    except Exception as e:
        print(f"Test failed: {e}")
        print("\nMake sure the CUA Collector is running (e.g. python collector.py) before running this script.")
