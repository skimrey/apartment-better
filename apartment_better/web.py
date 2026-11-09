import html
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, search
from .db import DB

PAGE = """<!doctype html><meta charset=utf-8><title>Apartment Better</title>
<style>body{font:15px system-ui;max-width:900px;margin:24px auto;padding:0 12px}input[type=text]{width:60%%;padding:8px}
.l{border:1px solid #ddd;border-radius:8px;padding:10px;margin:12px 0}.i{display:inline-block;margin:4px;font-size:12px;width:170px;vertical-align:top}
.i img{width:170px;height:120px;object-fit:cover;border-radius:4px}.m{color:#666;font-size:13px}</style>
<h2>Apartment Better</h2>
<form><input type=text name=q value="%(q)s" placeholder="second floor patio, lots of windows..." autofocus>
max $ <input name=maxp value="%(maxp)s" size=5> <label><input type=checkbox name=home %(home)s> home area only</label> <button>Search</button></form>
<p class=m>%(status)s</p>%(body)s"""


def serve(cfg: dict, db: DB, port: int = 8765) -> None:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(u.query)
            if u.path.startswith("/img/"):
                p = os.path.join(config.IMAGES, os.path.basename(u.path))
                if os.path.exists(p):
                    self.send_response(200); self.send_header("Content-Type", "image/jpeg"); self.end_headers()
                    self.wfile.write(open(p, "rb").read())
                else:
                    self.send_response(404); self.end_headers()
                return
            q = (qs.get("q") or [""])[0]
            maxp = (qs.get("maxp") or [""])[0]
            home = "home" in qs
            body = ""
            if q:
                res = search.search(cfg, db, q, 25, int(maxp) if maxp.isdigit() else None, None, home)
                for r in res:
                    pr = "-".join(str(x) for x in dict.fromkeys(r["price"]) if x) or "?"
                    imgs = "".join(
                        '<div class=i><img src="/img/%s"><br>%s</div>' % (
                            os.path.basename(e["path"] or ""), html.escape(e["match"] or "")) for e in r["photo_evidence"][:4])
                    body += '<div class=l><b><a href="%s" target=_blank>%s</a></b> <span class=m>%s | $%s | beds %s | %s | %d/%s photos described</span><br>%s%s</div>' % (
                        html.escape(r["url"]), html.escape(r["name"] or ""), html.escape(r["address"] or ""), pr, r["beds"],
                        r["source"], r["photos_described"], r["photos_total"], imgs,
                        ("<div class=m>listing text: %s</div>" % html.escape(r["listing_text_match"])) if r["listing_text_match"] else "")
                if not res:
                    body = "<p>No matches yet.</p>"
            st = db.status()
            page = PAGE % {"q": html.escape(q), "maxp": html.escape(maxp), "home": "checked" if home else "",
                           "status": "listings %s | photos %s" % (st["listings"], st["images"]), "body": body}
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers()
            self.wfile.write(page.encode())

    print("http://127.0.0.1:%d" % port, flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
