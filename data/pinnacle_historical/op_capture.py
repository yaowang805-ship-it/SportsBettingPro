"""捕获 OddsPortal /proxy/ajax-* 加密响应到本地文件, 供离线逆向解密格式。

用法:
    .venv312/bin/python data/pinnacle_historical/op_capture.py --sport football --out /tmp/opcap
"""
import sys, base64, argparse, json
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".venv312" / "lib" / "python3.12" / "site-packages"))

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")
BASE = "https://www.oddsportal.com"

HUBS = {"football": "/football/", "basketball": "/basketball/", "tennis": "/tennis/",
        "baseball": "/baseball/", "american-football": "/american-football/",
        "ice-hockey": "/ice-hockey/"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", default="football")
    ap.add_argument("--match-url", default=None)
    ap.add_argument("--out", default="/tmp/opcap")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    captured = []  # (url, body_bytes)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=CHROME,
            args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(user_agent=UA)
        page = ctx.new_page()
        page.set_default_timeout(60000)

        def on_response(resp):
            url = resp.url
            if "/proxy/ajax-" not in url:
                return
            try:
                captured.append((url, resp.body()))
            except Exception:
                pass

        page.on("response", on_response)

        match_url = args.match_url
        if not match_url:
            page.goto(BASE + HUBS.get(args.sport, f"/{args.sport}/"),
                      wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)
            links = page.eval_on_selector_all("a[href*='h2h']", "els => els.map(e => e.href)")
            match_url = next((l for l in links if "#" in l), links[0] if links else None)
            if not match_url:
                print("未找到 H2H 链接")
                return
        print(f"[match] {match_url}", flush=True)
        page.goto(match_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(8000)

        for tab in ["Over/Under", "Double Chance", "Draw No Bet", "BTTS", "Odd/Even",
                    "Asian Handicap", "Handicap"]:
            try:
                loc = page.locator("text=" + tab).first
                if loc.count() > 0:
                    loc.click(timeout=3000)
                    page.wait_for_timeout(2500)
            except Exception:
                pass

        browser.close()

    meta = []
    for i, (url, body) in enumerate(captured):
        fn = out_dir / f"{i:02d}.bin"
        fn.write_bytes(body)
        meta.append({"file": fn.name, "url": url, "len": len(body)})
        print(f"{fn.name}  len={len(body):6d}  {url.split('.com')[-1]}", flush=True)

    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    print(f"\n已存 {len(captured)} 个响应到 {out_dir}", flush=True)


if __name__ == "__main__":
    main()
