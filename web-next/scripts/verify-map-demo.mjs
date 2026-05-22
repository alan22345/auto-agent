// Local-verification harness for the capability/flow map UI.
//
// Drives the /map-demo route through every LOD and asserts the DOM
// shape + captures a screenshot per LOD so the user can visually
// confirm without spinning up the full backend.

import { chromium } from 'playwright';
import { mkdirSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';

const BASE = process.env.BASE_URL ?? 'http://localhost:3030';
const OUT = resolve(process.cwd(), 'verify-out');
mkdirSync(OUT, { recursive: true });

function step(name) {
  console.log(`\n→ ${name}`);
}

function assert(cond, msg) {
  if (!cond) {
    console.error(`  ✗ ${msg}`);
    process.exitCode = 1;
  } else {
    console.log(`  ✓ ${msg}`);
  }
}

async function main() {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  // Block the WS connection from the demo page. The Next.js root layout
  // mounts ``wsClient.connect()`` which proxies through to a backend
  // that isn't running locally — accumulated reconnect attempts wedge
  // the dev server. The Map view doesn't depend on the WS, so silencing
  // it keeps the verification quick + reliable.
  await page.route('**/ws', (route) => route.abort());
  await page.route('**/ws/**', (route) => route.abort());
  const errors = [];
  page.on('pageerror', (err) => errors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => {
    if (msg.type() === 'error') errors.push(`console error: ${msg.text()}`);
  });

  step('Load /map-demo');
  await page.goto(`${BASE}/map-demo`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('[data-testid="map-canvas"]', { timeout: 5000 });
  let lod = await page.getAttribute('[data-testid="map-canvas"]', 'data-lod');
  assert(lod === '0', `LOD 0 mounted (got data-lod="${lod}")`);

  // LOD 0 — capabilities visible
  step('LOD 0 — capabilities and Unreached tray');
  await page.waitForSelector('[data-testid="capability-tile-cap_auth"]');
  const authTile = await page.textContent('[data-testid="capability-tile-cap_auth"]');
  assert(/Authentication/.test(authTile), 'Authentication tile shows name');
  assert(/2 flows/.test(authTile), 'Authentication tile shows flow count');
  const carbonTile = await page.textContent(
    '[data-testid="capability-tile-cap_carbon"]',
  );
  assert(/Carbon Calc Engine/.test(carbonTile), 'Carbon tile shows name');
  const tray = await page.textContent('[data-testid="unreached-tray"]');
  assert(/Unreached \(3 nodes\)/.test(tray), 'Unreached tray shows node count');
  await page.screenshot({ path: `${OUT}/lod-0.png`, fullPage: true });

  // Drill into Authentication
  step('Drill into Authentication → LOD 1');
  await page.click('[data-testid="capability-tile-cap_auth"]');
  await page.waitForFunction(
    () => document.querySelector('[data-testid="map-canvas"]')?.getAttribute('data-lod') === '1',
    null,
    { timeout: 3000 },
  );
  lod = await page.getAttribute('[data-testid="map-canvas"]', 'data-lod');
  assert(lod === '1', `LOD 1 mounted (got data-lod="${lod}")`);
  await page.waitForSelector('[data-testid="flow-tile-flow_google_login"]');
  const googleTile = await page.textContent(
    '[data-testid="flow-tile-flow_google_login"]',
  );
  assert(/Google OAuth Login/.test(googleTile), 'Google flow tile shows name');
  assert(/HTTP/.test(googleTile), 'Google flow tile shows entry kind badge');
  assert(/3 steps/.test(googleTile), 'Google flow tile shows step count');
  // Breadcrumb has both segments
  const crumb1 = await page.textContent('[data-testid="map-breadcrumb"]');
  assert(/Capabilities/.test(crumb1) && /Authentication/.test(crumb1), 'Breadcrumb shows both segments');
  await page.screenshot({ path: `${OUT}/lod-1.png`, fullPage: true });

  // Drill into Google OAuth Login
  step('Drill into Google OAuth Login → LOD 2 step chain');
  await page.click('[data-testid="flow-tile-flow_google_login"]');
  await page.waitForFunction(
    () => document.querySelector('[data-testid="map-canvas"]')?.getAttribute('data-lod') === '2',
    null,
    { timeout: 3000 },
  );
  lod = await page.getAttribute('[data-testid="map-canvas"]', 'data-lod');
  assert(lod === '2', `LOD 2 mounted (got data-lod="${lod}")`);
  const chainTxt = await page.textContent('[data-testid="step-chain"]');
  assert(/login/.test(chainTxt), 'Step chain shows login');
  assert(/validate_token/.test(chainTxt), 'Step chain shows validate_token');
  assert(/create/.test(chainTxt), 'Step chain shows create');
  // Branch indicator on validate_token (is_branch_root=true in the demo blob)
  const branchAttr = await page.getAttribute(
    '[data-testid="step-card-lib/oauth.py::validate_token"]',
    'data-branch',
  );
  assert(branchAttr === 'true', 'validate_token marked as branch root');
  // Boundary port to the sibling Email Signup flow (shares lib/sessions.py::create)
  await page.waitForSelector('[data-testid="boundary-ports-row"]');
  const portsTxt = await page.textContent('[data-testid="boundary-ports-row"]');
  assert(/Also used in/.test(portsTxt), 'Boundary ports row header');
  assert(/Email Signup/.test(portsTxt), 'Sibling flow port shown for Email Signup');
  await page.screenshot({ path: `${OUT}/lod-2.png`, fullPage: true });

  // Drill into a step → LOD 3 source preview
  step('Drill into a step → LOD 3 source preview');
  await page.click('[data-testid="step-card-app/auth/google.py::login"]');
  await page.waitForFunction(
    () => document.querySelector('[data-testid="map-canvas"]')?.getAttribute('data-lod') === '3',
    null,
    { timeout: 3000 },
  );
  lod = await page.getAttribute('[data-testid="map-canvas"]', 'data-lod');
  assert(lod === '3', `LOD 3 mounted (got data-lod="${lod}")`);
  const previewTxt = await page.textContent('[data-testid="map-source-preview"]');
  assert(/login/.test(previewTxt), 'Source preview header includes node label');
  assert(/app\/auth\/google\.py/.test(previewTxt), 'Source preview header includes file');
  await page.screenshot({ path: `${OUT}/lod-3.png`, fullPage: true });

  // URL state — confirm focus path is encoded into the URL
  const urlNow = page.url();
  assert(
    urlNow.includes('p=cap_auth%2Fflow_google_login%2F') ||
      urlNow.includes('p=cap_auth/flow_google_login/'),
    `URL encodes focus path (${urlNow})`,
  );

  // Keyboard nav — Esc drills out one LOD
  step('Press Esc → drill out one LOD');
  await page.keyboard.press('Escape');
  await page.waitForFunction(
    () => document.querySelector('[data-testid="map-canvas"]')?.getAttribute('data-lod') === '2',
    null,
    { timeout: 3000 },
  );
  lod = await page.getAttribute('[data-testid="map-canvas"]', 'data-lod');
  assert(lod === '2', `Esc drilled out to LOD 2 (got data-lod="${lod}")`);

  // Home returns to LOD 0
  step('Press Home → return to LOD 0');
  await page.keyboard.press('Home');
  await page.waitForFunction(
    () => document.querySelector('[data-testid="map-canvas"]')?.getAttribute('data-lod') === '0',
    null,
    { timeout: 3000 },
  );
  lod = await page.getAttribute('[data-testid="map-canvas"]', 'data-lod');
  assert(lod === '0', `Home returned to LOD 0 (got data-lod="${lod}")`);

  // Deep link — load directly into LOD 2 by URL. Uses a hard reload so
  // there's no prior router state to interfere with the deep-link
  // path. Verifies both that the URL → focus parser works and that the
  // LOD 1 → 2 → 3 render path is reachable without clicking.
  step('Deep link straight into LOD 2');
  await page.goto(`${BASE}/map-demo?p=cap_auth/flow_google_login`, {
    waitUntil: 'domcontentloaded',
  });
  await page.waitForSelector('[data-testid="map-canvas"]');
  lod = await page.getAttribute('[data-testid="map-canvas"]', 'data-lod');
  assert(lod === '2', `Deep link landed on LOD 2 (got data-lod="${lod}")`);

  // Deep link straight into LOD 3
  step('Deep link straight into LOD 3');
  await page.goto(
    `${BASE}/map-demo?p=cap_auth/flow_google_login/${encodeURIComponent('app/auth/google.py::login')}`,
    { waitUntil: 'domcontentloaded' },
  );
  await page.waitForSelector('[data-testid="map-canvas"]');
  lod = await page.getAttribute('[data-testid="map-canvas"]', 'data-lod');
  assert(lod === '3', `Deep link landed on LOD 3 (got data-lod="${lod}")`);
  const previewTxt2 = await page.textContent('[data-testid="map-source-preview"]');
  assert(/app\/auth\/google\.py/.test(previewTxt2), 'Deep-linked LOD 3 source preview shows correct file');

  await browser.close();

  if (errors.length > 0) {
    console.error('\nBrowser errors during run:');
    for (const e of errors) console.error(`  - ${e}`);
    process.exitCode = 1;
  }

  const summary = `Screenshots written to ${OUT}/lod-{0,1,2,3}.png`;
  writeFileSync(`${OUT}/SUMMARY.txt`, summary + '\n');
  console.log(`\n${summary}`);
  if (process.exitCode === 1) {
    console.log('FAILED — see assertions above.');
    process.exit(1);
  } else {
    console.log('All assertions passed.');
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
