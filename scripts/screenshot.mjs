// Headless screenshot of a page that renders client-side (Grafana, Alertmanager).
// usage: node scripts/screenshot.mjs <url> <out.png> [width] [height]
import puppeteer from "puppeteer-core";

const [url, out, width = "1600", height = "1000"] = process.argv.slice(2);
const browser = await puppeteer.launch({
  executablePath: process.env.CHROME_PATH || "/usr/bin/google-chrome",
  args: ["--no-sandbox", "--disable-dev-shm-usage"],
});
try {
  const page = await browser.newPage();
  await page.setViewport({ width: Number(width), height: Number(height) });
  await page.goto(url, { waitUntil: "networkidle2", timeout: 60_000 });
  await new Promise((resolve) => setTimeout(resolve, 8_000)); // let panels finish querying and animating
  await page.screenshot({ path: out });
} finally {
  await browser.close();
}
