"""Launcher AutoClipper: jalankan server lokal + buka browser."""
import threading
import time
import webbrowser

import uvicorn

PORT = 8765


def _open_browser() -> None:
    time.sleep(1.2)  # tunggu server siap
    webbrowser.open(f"http://127.0.0.1:{PORT}")


if __name__ == "__main__":
    threading.Thread(target=_open_browser, daemon=True).start()
    print("=" * 56)
    print("  AutoClipper berjalan di  http://127.0.0.1:%d" % PORT)
    print("  Tekan Ctrl+C untuk berhenti")
    print("=" * 56)
    uvicorn.run("backend.main:app", host="127.0.0.1", port=PORT, log_level="warning")
