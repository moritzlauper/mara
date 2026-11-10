// Requires Playwright. Start `python3 docs/readme_demo.py`, then run this script.
// Captures the actual Mix UI with fictional data and presentation-only framing.
const { chromium } = require('playwright');
const path = require('node:path');

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1200 }, deviceScaleFactor: 2 });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto('http://127.0.0.1:8876/workflow?demo=mix');
    await page.locator('#latest-changes li').first().waitFor();
    await page.evaluate(() => document.fonts.ready);
    await page.addStyleTag({ content: `
      :root { color-scheme: light; }
      body { padding: 28px; }
      .app-header { max-width: 1080px; margin: 0 auto; padding: 18px 24px; border: 2px solid var(--hi); border-bottom: 3px solid var(--ink); }
      .app-header > :not(.logo):not(.badge):not(.teaser-caption) { display: none!important; }
      #connection { display: none!important; }
      .app-header .badge { margin-left: auto; order: 3; }
      .teaser-caption { font: 16px var(--sans); margin-left: 12px; }
      aside, footer, #message, #pause-note, .mix-more, section > details { display: none!important; }
      .app-shell { display: block; min-height: 0; }
      main { padding: 22px 0 0; }
      section { max-width: none; padding-bottom: 20px; }
      #mix-title { font-size: 28px; }
      #mix-stage-purpose, #mix-description { font-size: 15px; }
      #mix-description { margin-bottom: 18px; }
      .closeness-word { font-size: 30px; }
      .dashboard-window p.muted, .latest-changes li, #monitor-context, #progress-label { font-size: 14px; }
      .latest-changes { margin-bottom: 0; }
      .latest-changes h2 { font-size: 16px; }
      * { animation: none!important; transition: none!important; }
    ` });
    await page.evaluate(() => {
      document.documentElement.dataset.theme = 'light';
      const caption = document.createElement('span');
      caption.className = 'teaser-caption';
      caption.textContent = 'Free reference-guided mixing for Ableton Live';
      document.querySelector('.logo').after(caption);
    });
    const bounds = await page.locator('[data-panel="mix"]').boundingBox();
    if (errors.length) throw new Error(errors.join('\n'));
    await page.screenshot({ path: path.join(__dirname, 'images/mara-teaser.png'),
      clip: { x: bounds.x - 28, y: 0, width: bounds.width + 56, height: Math.ceil(bounds.y + bounds.height + 30) } });
    console.log('Saved docs/images/mara-teaser.png');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
