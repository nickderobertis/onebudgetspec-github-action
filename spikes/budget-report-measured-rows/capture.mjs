// Throwaway visual spike: render each fixture here to a PNG beside it. Run from anywhere.
import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const host = process.env.CAPTURE_HOST_ROOT ?? '/home/whoop/ai-orchestrator';
const require = createRequire(join(host, 'package.json'));
const { marked } = require('marked');
const { chromium } = require('playwright');

const style = `body{font:14px/1.5 -apple-system,"Segoe UI",Helvetica,Arial,sans-serif;color:#1f2328;margin:24px;max-width:1392px}
h2{border-bottom:1px solid #d1d9e0;padding-bottom:.3em}table{border-collapse:collapse;margin:16px 0}
th,td{border:1px solid #d1d9e0;padding:6px 13px}th{font-weight:600}tr:nth-child(2n){background:#f6f8fa}`;

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
for (const file of readdirSync(here).filter((name) => name.endsWith('.md'))) {
  await page.setContent(`<!doctype html><meta charset="utf-8"><style>${style}</style>${marked.parse(readFileSync(join(here, file), 'utf8'))}`);
  await page.screenshot({ path: join(here, file.replace(/\.md$/, '.png')), fullPage: true });
}
await browser.close();
