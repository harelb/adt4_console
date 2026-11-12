#!/usr/bin/env node
// Browser test of the Missions page on a recorded fixture mission (mission logging C1/C2).
//
//   CONSOLE_URL=http://127.0.0.1:8099/ MISSION_ID=m... SHOT_DIR=/tmp/shots node tests/missions-browser.mjs
//
// tests/test_missions_browser.py (backend) builds the synthetic two-machine mission,
// collects it and serves the real console on it, then runs this: the list, the
// mission's timeline (click an event), goals, replay (play + scrub), chat, stats
// and check tabs; fails on any JavaScript error or a missing element.
import {chromium} from "playwright-core";
import {existsSync, mkdirSync} from "node:fs";
import {homedir} from "node:os";
import {join} from "node:path";

const base = process.env.CONSOLE_URL ?? "http://127.0.0.1:8099/";
const mid = process.env.MISSION_ID;
const shotDir = process.env.SHOT_DIR ?? "";
if (shotDir) mkdirSync(shotDir, {recursive: true});
if (!mid) throw new Error("MISSION_ID is required");

function findChrome() {
  if (process.env.CHROMIUM_PATH) return process.env.CHROMIUM_PATH;
  const pw = join(homedir(), ".cache", "ms-playwright");
  for (const c of [join(pw, "chromium-1234", "chrome-linux64", "chrome"), "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"])
    if (existsSync(c)) return c;
  return chromium.executablePath();
}

const browser = await chromium.launch({executablePath: findChrome(), headless: true, args: ["--no-sandbox", "--disable-dev-shm-usage"]});
const page = await browser.newPage({viewport: {width: 1500, height: 1000}});
const errors = [];
page.on("pageerror", e => errors.push(String(e)));
page.on("console", m => m.type() === "error" && !/favicon|WebSocket|ws:\/\//i.test(m.text()) && errors.push(m.text()));
const url = route => {
  const u = new URL(base);
  u.hash = `#${route}`;
  return u.toString();
};
const shot = async name => shotDir && page.screenshot({path: join(shotDir, `${name}.png`), fullPage: true});
const tab = name => page.getByRole("tab", {name, exact: true});
const steps = [];
const step = async (name, fn) => {
  await fn();
  steps.push(name);
  await shot(name);
};

try {
  await step("list", async () => {
    await page.goto(url("/missions"));
    await page.waitForSelector(`[data-testid="mission-row-${mid}"]`, {timeout: 15000});
    const row = await page.textContent(`[data-testid="mission-row-${mid}"]`);
    if (!/pass/.test(row)) throw new Error(`list row has no PASS check: ${row}`);
  });
  await step("timeline", async () => {
    await page.click(`[data-testid="mission-row-${mid}"]`);
    await page.waitForSelector(".mdot", {timeout: 15000});
    const n = await page.locator(".mdot").count();
    if (n < 10) throw new Error(`timeline has only ${n} events`);
    await page.locator('.mdot[aria-label="lease.intervention"]').first().click();
    await page.waitForFunction(() => document.querySelector(".mevent-detail")?.textContent?.includes("manual"));
  });
  await step("goals", async () => {
    await tab("Goals").click();
    await page.waitForSelector(".mgoal-robot", {timeout: 10000});
    const t = await page.textContent(".mgoals");
    if (!/plan-0001/.test(t) || !/done/.test(t)) throw new Error(`goals tab: ${t.slice(0, 200)}`);
    await page.waitForSelector(".mgoals img", {timeout: 10000});
  });
  await step("replay", async () => {
    await tab("Replay").click();
    await page.waitForSelector('svg[aria-label="Mission replay map"]', {timeout: 10000});
    const scrub = page.locator('input[aria-label="Replay time"]');
    const max = Number(await scrub.getAttribute("max"));
    const min = Number(await scrub.getAttribute("min"));
    await scrub.fill(String(min + 0.4 * (max - min)));
    await page.waitForFunction(() => document.querySelectorAll('g[data-robot="euclid"] polyline').length >= 1);
    await page.getByRole("tab", {name: "20x"}).click();
    await page.getByRole("button", {name: "Play"}).click();
    await page.waitForTimeout(800);
    await page.getByRole("button", {name: "Pause"}).click();
  });
  await step("chat", async () => {
    await tab("Chat").click();
    await page.waitForSelector(".mchat-msg", {timeout: 10000});
  });
  await step("stats", async () => {
    await tab("Stats").click();
    await page.waitForSelector(".mstats table", {timeout: 10000});
  });
  await step("check", async () => {
    await tab("Check").click();
    await page.waitForSelector('[data-check="secrets"]', {timeout: 10000});
    await page.locator(".mtab button.btn-primary").click();
    await page.waitForTimeout(500);
  });
  if (errors.length) throw new Error(`JavaScript errors:\n${errors.join("\n")}`);
  console.log(JSON.stringify({ok: true, steps}));
} catch (e) {
  await shot("failure");
  console.error(`FAILED after ${steps.join(", ")}: ${e.message}`);
  process.exitCode = 1;
} finally {
  await browser.close();
}
