// Record the dashboard while replay.py plays one run's events into it, for docs/factory-run.gif.
//
//   python dashboard/server.py --port 8790 --events /tmp/sf-gif/events.jsonl &
//   node docs/gif/record.mjs [seconds] [run]        # needs `npm i playwright`
//   ffmpeg -i /tmp/sf-gif/video/*.webm ...           # crop, scale and convert to GIF
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..', '..');
const out = process.env.SF_GIF_DIR || '/tmp/sf-gif';
const secs = Number(process.argv[2] || 60);
const run = process.argv[3] || 'issue-29';

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1600, height: 900 }, recordVideo: { dir: `${out}/video`, size: { width: 1600, height: 900 } } });
const page = await ctx.newPage();
await page.goto(process.env.SF_DASHBOARD_URL || 'http://localhost:8790/');
await page.waitForTimeout(2500);
const p = spawn('python3', [path.join(here, 'replay.py'), path.join(root, 'runs', 'events.jsonl'), `${out}/events.jsonl`, run], { stdio: 'inherit' });
await page.waitForTimeout(secs * 1000);
await page.screenshot({ path: `${out}/end.png` });
await ctx.close(); await browser.close(); p.kill();
console.log('recorded');
