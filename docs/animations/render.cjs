// Render the README animations to GIF.
//
//   node docs/animations/render.cjs                 # all scenes -> docs/img/*.gif
//   node docs/animations/render.cjs 02-query-flow   # one scene
//   node docs/animations/render.cjs 02-query-flow --still 4.5,9   # PNG stills for checking
//
// Needs Node, Playwright (Chromium) and ffmpeg. Each frame is rendered by
// calling window.seek(t), so output is deterministic.

const { chromium } = require("playwright");
const { execFileSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const HERE = __dirname;
const OUT = path.join(HERE, "..", "img");
const FPS = 15;
const OUT_WIDTH = 1000;

async function render(page, scene, stills) {
  await page.goto("file://" + path.join(HERE, `${scene}.html`));
  await page.evaluate(() => document.fonts.ready);
  const duration = await page.evaluate(() => window.getDuration());

  if (stills) {
    for (const t of stills) {
      await page.evaluate((t) => window.seek(t), t);
      const file = path.join(OUT, `${scene}@${t}s.png`);
      await page.screenshot({ path: file });
      console.log("still", file);
    }
    return;
  }

  const dir = fs.mkdtempSync(path.join(os.tmpdir(), `frames-${scene}-`));
  const n = Math.ceil(duration * FPS);
  for (let i = 0; i <= n; i++) {
    await page.evaluate((t) => window.seek(t), i / FPS);
    await page.screenshot({ path: path.join(dir, `${String(i).padStart(4, "0")}.png`) });
  }
  const gif = path.join(OUT, `${scene}.gif`);
  execFileSync("ffmpeg", [
    "-y", "-loglevel", "error", "-framerate", String(FPS), "-i", path.join(dir, "%04d.png"),
    "-vf", `scale=${OUT_WIDTH}:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];` +
           `[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle`,
    "-loop", "0", gif,
  ]);
  fs.rmSync(dir, { recursive: true, force: true });
  console.log(`${gif}  ${duration.toFixed(1)}s  ${(fs.statSync(gif).size / 1e6).toFixed(2)} MB`);
}

(async () => {
  const args = process.argv.slice(2);
  const stillIdx = args.indexOf("--still");
  const stills = stillIdx >= 0 ? args[stillIdx + 1].split(",").map(Number) : null;
  const named = args.filter((a, i) => !a.startsWith("--") && !(stillIdx >= 0 && i === stillIdx + 1));
  const scenes = named.length
    ? named
    : fs.readdirSync(HERE).filter((f) => /^\d\d-.*\.html$/.test(f)).map((f) => f.replace(".html", "")).sort();

  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1200, height: 675 } });
  for (const s of scenes) await render(page, s, stills);
  await browser.close();
})();
