"""Drives a normal Chrome window that we start ourselves with a debugging port and its own profile."""
import os
import socket
import subprocess
import time
from typing import Optional

from playwright.sync_api import sync_playwright

from . import config


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def ensure_chrome(cfg: dict) -> int:
    ch = cfg["chrome"]
    port = ch["port"]
    if _port_open(port):
        return port
    profile = os.path.join(config.ROOT, ch["profile"])
    os.makedirs(profile, exist_ok=True)
    subprocess.Popen(
        [ch["path"], "--remote-debugging-port=%d" % port, "--user-data-dir=" + profile,
         "--no-first-run", "--no-default-browser-check", "--window-size=1100,800", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(60):
        if _port_open(port):
            return port
        time.sleep(0.5)
    raise RuntimeError("Chrome did not open its debugging port")


class Browser:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.pw = sync_playwright().start()
        port = ensure_chrome(cfg)
        self.browser = self.pw.chromium.connect_over_cdp("http://127.0.0.1:%d" % port)
        self.ctx = self.browser.contexts[0]
        self.page = self.ctx.new_page()

    def html(self, url: str, wait_ms: int = 2500) -> str:
        """Page HTML after load. Raises if the site keeps refusing the page."""
        for attempt, cool in enumerate((0, 120, 300, 600, 900, 1800)):
            if cool:
                time.sleep(cool)  # blocked: cool off for minutes before trying again
            r = self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            self.page.wait_for_timeout(wait_ms)
            h = self.page.content()
            if r is not None and r.status == 200 and "Access Denied" not in h[:3000]:
                return h
        raise RuntimeError("apartments.com blocked: " + url)

    def fetch(self, url: str, referer: str = "https://www.apartments.com/") -> Optional[bytes]:
        r = self.ctx.request.get(url, headers={"Referer": referer}, timeout=60000)
        return r.body() if r.ok else None

    def close(self) -> None:
        try:
            self.page.close()
        finally:
            self.pw.stop()
