import os, asyncio, base64, time, subprocess
from playwright.async_api import async_playwright
import imageio_ffmpeg

FF = imageio_ffmpeg.get_ffmpeg_exe()
OUT = "/workspace/ffl/pack_circle.mp4"
DURATION = 5.0
LOOPS = 2                     # two full revolutions in 5 s (2.5 s per loop, steady speed)
M = 72                        # css px margin around the canvas kept in frame (background + glow)
SFRAMES = "/workspace/ffl/frames"
os.makedirs(SFRAMES, exist_ok=True)
for f in os.listdir(SFRAMES):
    os.remove(os.path.join(SFRAMES, f))

INJECT = """(() => {
  const btn = () => document.querySelector('.pk-pack');
  window.__circle = (cx, cy, radius, periodMs, delayMs) => {
    const b = btn();
    b.dispatchEvent(new PointerEvent('pointerenter', {pointerType:'mouse', clientX:cx, clientY:cy, bubbles:true}));
    const t0 = performance.now() + delayMs;
    const step = () => {
      const t = (performance.now() - t0) / periodMs;
      if (t > 1) return;
      if (t >= 0) {
        const th = 2*Math.PI*t - Math.PI/2;            // start at top, clockwise
        const x = cx + radius*Math.cos(th), y = cy + radius*Math.sin(th);
        b.dispatchEvent(new PointerEvent('pointermove', {pointerType:'mouse', clientX:x, clientY:y, bubbles:true}));
      }
      requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
    return 'running';
  };
})();"""

async def main():
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = "/workspace/ffl/sysroot/usr/lib/x86_64-linux-gnu:/workspace/ffl/sysroot/lib/x86_64-linux-gnu"
    env["FONTCONFIG_FILE"] = "/workspace/ffl/custom-fonts.conf"

    frames = []          # (idx, epoch_seconds)
    counter = {"n": 0}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, env=env)
        ctx = await browser.new_context(viewport={"width":1200,"height":900}, device_scale_factor=2)
        page = await ctx.new_page()
        await page.goto("https://flatfootlabs.com/", wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(2000)

        hidden = await page.evaluate("""() => {
            const out = [];
            for (const sel of ['.pk-count', '.pk-hint', '.pk-links', '.nx-cards-copy']) {
                const el = document.querySelector(sel);
                if (el && getComputedStyle(el).display !== 'none') {
                    el.style.setProperty('visibility', 'hidden', 'important');
                    out.push(sel);
                }
            }
            return out;
        }""")
        print("hidden:", hidden)
        await page.wait_for_timeout(300)
        geo = await page.evaluate("""() => {
            const c = document.querySelector('canvas.pk-gl');
            const r = c.getBoundingClientRect();
            window.scrollTo({top: window.scrollY + r.y + r.height/2 - innerHeight/2, behavior:'instant'});
            const r2 = c.getBoundingClientRect();
            return {x: r2.x, y: r2.y, w: r2.width, h: r2.height, ready: document.querySelector('.pk').className};
        }""")
        assert "is-ready" in geo["ready"] and "is-gl" in geo["ready"], geo["ready"]
        cx, cy = geo["x"] + geo["w"]/2, geo["y"] + geo["h"]/2
        radius = geo["w"] / 8.0        # radius 48 css px => diameter 96 = canvas width / 4
        crop_css = (geo["x"] - M, geo["y"] - M, geo["w"] + 2*M, geo["h"] + 2*M)
        print("geo", geo, "| center", cx, cy, "| radius", radius, "| crop", crop_css)
        await page.wait_for_timeout(1000)   # settle after the scroll

        cdp = await ctx.new_cdp_session(page)

        def on_frame(params):
            idx = counter["n"]; counter["n"] += 1
            data = params["data"]
            with open(f"{SFRAMES}/f{idx:05d}.jpg", "wb") as f:
                f.write(base64.b64decode(data))
            ts = params.get("metadata", {}).get("timestamp")
            frames.append((idx, ts if ts is not None else time.time()))
            asyncio.ensure_future(cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]}))

        cdp.on("Page.screencastFrame", on_frame)
        await cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 92, "maxWidth": 2400, "maxHeight": 1800, "everyFrame": True})
        await page.wait_for_timeout(1200)

        await page.evaluate(INJECT)
        t_start = time.time() + 0.3     # driver starts 300 ms after this eval returns
        state = await page.evaluate("window.__circle(%r, %r, %r, %r, 300)" % (cx, cy, radius, DURATION/LOOPS*1000))
        print("driver:", state, "t_start", t_start, "epoch now", time.time())
        await page.wait_for_timeout(int((DURATION + 0.6) * 1000))
        await cdp.send("Page.stopScreencast")
        print("frames total:", counter["n"])
        await ctx.close()
        await browser.close()

    used = [(i, t) for i, t in frames if t is not None and t_start + 0.05 <= t <= t_start + DURATION + 0.05]
    if used:
        print("used frames:", len(used), "span %.3fs" % (used[-1][1] - used[0][1]))
    else:
        raise SystemExit("no frames selected")
    ts = [t for _, t in used]
    with open("/workspace/ffl/frames.txt", "w") as f:
        for i in range(len(used)):
            d = (ts[i+1] - ts[i]) if i + 1 < len(ts) else 1/30
            d = max(min(d, 0.25), 0.004)
            f.write(f"file '{SFRAMES}/f{used[i][0]:05d}.jpg'\nduration {d:.6f}\n")
        f.write(f"file '{SFRAMES}/f{used[-1][0]:05d}.jpg'\n")

    sx, sy, sw, sh = int(crop_css[0]), int(round(crop_css[1])), int(crop_css[2]), int(crop_css[3])
    cmd = [FF, "-y", "-f", "concat", "-safe", "0", "-i", "/workspace/ffl/frames.txt",
           "-vf", f"crop={sw}:{sh}:{sx}:{sy},fps=30", "-an", "-c:v", "libx264",
           "-preset", "slow", "-crf", "16", "-pix_fmt", "yuv420p", "-movflags", "+faststart", OUT]
    print(" ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-2500:])
    else:
        print("OK", os.path.getsize(OUT), "bytes")

asyncio.run(main())
