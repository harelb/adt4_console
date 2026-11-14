#!/usr/bin/env node
// Screenshots + button geometry of the chat's PDDL goal card against the mock
// server (npm run mock): `node scripts/chat-card-shots.mjs <outdir> <tag>`.
// Reports gaps between the card's buttons and whether a send button sits
// under the pointer after Preview is clicked.
import {chromium} from "playwright-core";
import {existsSync, mkdirSync, writeFileSync} from "node:fs";
import {homedir} from "node:os";
import {join} from "node:path";

const base = process.env.CONSOLE_URL ?? "http://127.0.0.1:8090/";
const out = process.argv[2] ?? "shots";
const tag = process.argv[3] ?? "run";
mkdirSync(out, {recursive: true});
const chrome = [join(homedir(), ".cache", "ms-playwright", "chromium-1234", "chrome-linux64", "chrome"), "/usr/bin/google-chrome"].find(existsSync);
const browser = await chromium.launch({executablePath: chrome, headless: true, args: ["--no-sandbox", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"]});

const VIEWPORTS = [
  {name: "mbp14", width: 1512, height: 982},
  {name: "mbp16", width: 1728, height: 1117},
];
const PANES = [
  {name: "default", rightW: 380},
  {name: "narrow", rightW: 280},
];

async function boxes(page) {
  return page.evaluate(() => {
    const card = document.querySelector(".msg-goal");
    if (!card) return null;
    return [...card.querySelectorAll("button")].map(b => {
      const r = b.getBoundingClientRect();
      return {text: b.textContent.trim(), x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)};
    });
  });
}
const isSend = t => /^Send|Sending/.test(t);
function analyse(bs) {
  const issues = [];
  for (let i = 0; i < bs.length; i++)
    for (let j = i + 1; j < bs.length; j++) {
      const a = bs[i], b = bs[j];
      const sameRow = a.y < b.y + b.h && b.y < a.y + a.h;
      const sameCol = a.x < b.x + b.w && b.x < a.x + a.w;
      const gx = sameRow ? Math.max(b.x - (a.x + a.w), a.x - (b.x + b.w)) : null;
      const gy = sameCol ? Math.max(b.y - (a.y + a.h), a.y - (b.y + b.h)) : null;
      const gap = Math.min(gx ?? Infinity, gy ?? Infinity);
      if (gap === Infinity) continue;
      if (gap < 12 && (isSend(a.text) || isSend(b.text))) issues.push({a: a.text, b: b.text, gap});
    }
  return issues;
}

const report = [];
try {
  for (const vp of VIEWPORTS)
    for (const pane of PANES) {
      const ctx = await browser.newContext({viewport: {width: vp.width, height: vp.height}, deviceScaleFactor: 2});
      await ctx.addInitScript(w => {
        try {
          localStorage.setItem("adt4.viewer.layout.v1", JSON.stringify({leftW: 280, rightW: w, windows: {}, order: [], drive: null}));
        } catch {}
      }, pane.rightW);
      const page = await ctx.newPage();
      await page.goto(new URL("#/viewer", base).toString(), {waitUntil: "domcontentloaded"});
      await page.waitForTimeout(2500);
      await page.getByRole("tab", {name: "Chat"}).first().click().catch(async () => page.getByText("Chat", {exact: true}).first().click());
      await page.waitForTimeout(500);
      const box = page.locator(".chat-input textarea, .chat-input input").first();
      await box.fill("send both robots: pddl team goal");
      await box.press("Enter");
      await page.waitForSelector(".msg-goal", {timeout: 10000});
      await page.waitForTimeout(800);
      const id = `${tag}_${vp.name}_${pane.name}`;
      const card = page.locator(".msg-goal").first();
      await page.locator(".chat-log").screenshot({path: join(out, `${id}_1log.png`)});
      await card.screenshot({path: join(out, `${id}_1card.png`)});
      const before = await boxes(page);
      const pv = page.getByRole("button", {name: "Preview"}).first();
      const pb = await pv.boundingBox();
      const cx = pb.x + pb.width / 2, cy = pb.y + pb.height / 2;
      await page.mouse.click(cx, cy);
      const previewed = await page.waitForSelector("[data-testid=goal-preview]", {timeout: 6000}).then(
        () => true,
        () => false,
      );
      if (!previewed) {
        await card.screenshot({path: join(out, `${id}_2FAIL.png`)});
        const sentText = (await card.innerText()).match(/Sent[^\n]*/)?.[0] ?? null;
        report.push({id, viewport: vp, rightW: pane.rightW, previewClickSent: sentText, before: await boxes(page), closeBefore: analyse(await boxes(page))});
        console.log(id, "| clicking Preview did NOT preview; card says:", sentText);
        await ctx.close();
        continue;
      }
      await page.waitForTimeout(400);
      const under = await page.evaluate(([x, y]) => document.elementFromPoint(x, y)?.closest("button")?.textContent?.trim() ?? null, [cx, cy]);
      await card.screenshot({path: join(out, `${id}_2preview.png`)});
      const after = await boxes(page);
      const direct = page.getByRole("button", {name: /Send route directly/}).first();
      const db = await direct.boundingBox();
      const dx = db.x + db.width / 2, dy = db.y + db.height / 2;
      await page.mouse.click(dx, dy);
      await page.waitForTimeout(300);
      const underDirect = await page.evaluate(([x, y]) => document.elementFromPoint(x, y)?.closest("button")?.textContent?.trim() ?? null, [dx, dy]);
      await card.screenshot({path: join(out, `${id}_3confirm.png`)});
      const confirm = await boxes(page);
      await page.screenshot({path: join(out, `${id}_0full.png`)});
      // the PDDL goal text stays inside its card
      // anything drawn past its bubble's right edge (a label or a PDDL line)
      const overflow = await page.evaluate(() => {
        const bad = [];
        for (const m of document.querySelectorAll(".chat-log .msg")) {
          const edge = m.getBoundingClientRect().right;
          const scrolls = getComputedStyle(m).overflowX !== "visible";
          if (m.scrollWidth - m.clientWidth > 1 && !scrolls) bad.push({cls: m.className, over: m.scrollWidth - m.clientWidth});
          for (const el of m.querySelectorAll("*")) {
            const r = el.getBoundingClientRect();
            const range = document.createRange();
            range.selectNodeContents(el);
            const tr = range.getBoundingClientRect();
            const right = Math.max(r.right, el.children.length ? 0 : tr.right);
            if (right > edge + 1 && !el.closest("[style*=overflow], pre") ) bad.push({cls: m.className.slice(0, 20), el: el.tagName + "." + el.className, text: el.textContent.slice(0, 40), over: Math.round(right - edge)});
          }
        }
        return bad.slice(0, 8);
      });
      report.push({id, viewport: vp, rightW: pane.rightW, underPointerAfterPreview: under, underPointerAfterDirect: underDirect, before, closeBefore: analyse(before), after, closeAfter: analyse(after), confirm, closeConfirm: analyse(confirm), overflow});
      await ctx.close();
    }
} finally {
  await browser.close();
}
writeFileSync(join(out, `${tag}_report.json`), JSON.stringify(report, null, 1));
for (const r of report.filter(r => !r.previewClickSent))
  console.log(r.id, "| under pointer after Preview:", r.underPointerAfterPreview, "| after direct:", r.underPointerAfterDirect, "| close send pairs:", JSON.stringify([...r.closeBefore, ...r.closeAfter, ...r.closeConfirm]), "| overflow:", JSON.stringify(r.overflow));
