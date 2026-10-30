#!/usr/bin/env node
/**
 * ck_qr.js — 服务器侧无头浏览器扫码登录，把 pt_key/pt_pin 抠出来。
 *
 * 用法: node ck_qr.js <session_dir>
 *
 * 为什么非得开浏览器：京东早期那套纯 HTTP 的 qr_id → check → login.action
 * 已经废了（qr.m.jd.com/show 不带会话 cookie 返回的是占位图，check 直接
 * code=257 参数异常）。现在二维码跟浏览器会话绑定，必须在真实浏览器里渲染，
 * 扫完的 cookie 也落在那个浏览器里。
 *
 * 输出都写到 <session_dir>：
 *   qr.png     二维码截图
 *   status.json {"state":"qr|ok|expired","msg":"..."}
 *   ck.json    {"pt_key":"...","pt_pin":"..."}   仅 state=ok 时存在
 */
const fs = require('fs');
const path = require('path');
const puppeteer = require('puppeteer-core');

const DIR = process.argv[2];
if (!DIR) { console.error('usage: node ck_qr.js <session_dir>'); process.exit(2); }
fs.mkdirSync(DIR, { recursive: true });

// 固定 profile：每次扫码都复用同一套 cookie/缓存/指纹，京东那边看到的是
// “同一台设备又登录了一次”，而不是“又冒出一台新设备”。这个区别在风控上很大。
const PROFILE = process.env.QR_PROFILE || path.join(path.dirname(DIR), 'profile');
fs.mkdirSync(PROFILE, { recursive: true });

const CHROME = process.env.CHROME_PATH || '/usr/bin/chromium';
const WAIT_MS = Number(process.env.QR_TIMEOUT || 180) * 1000;

const writeStatus = (state, msg = '') => {
  fs.writeFileSync(path.join(DIR, 'status.json'),
    JSON.stringify({ state, msg, ts: Date.now() }));
};

const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
           '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36';

(async () => {
  writeStatus('starting');
  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: 'new',
    userDataDir: PROFILE,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu',
           '--lang=zh-CN', '--window-size=460,820'],
  });

  try {
    const page = await browser.newPage();
    await page.setUserAgent(UA);
    await page.setViewport({ width: 460, height: 820 });
    await page.goto('https://passport.jd.com/new/login.aspx',
                    { waitUntil: 'networkidle2', timeout: 45000 });

    // 固定 profile 里可能已经有上一次的登录态。先验证它还有效——
    // 有效就直接复用，不产生新的登录事件（对账号最安全的一种“续期”）。
    const pre = await page.cookies(
      'https://www.jd.com', 'https://passport.jd.com', 'https://home.m.jd.com');
    const preKey = pre.find((c) => c.name === 'pt_key');
    const prePin = pre.find((c) => c.name === 'pt_pin');
    if (preKey && prePin) {
      try {
        await page.goto('https://me-api.jd.com/user_new/info/GetJDUserInfoUnion',
                        { waitUntil: 'domcontentloaded', timeout: 20000 });
        const body = await page.evaluate(() => document.body.innerText);
        if (/"retcode"\s*:\s*"?0"?/.test(body)) {
          fs.writeFileSync(path.join(DIR, 'ck.json'), JSON.stringify({
            pt_key: preKey.value,
            pt_pin: decodeURIComponent(prePin.value),
            reused: true,
          }));
          writeStatus('ok', 'reused');
          return;
        }
      } catch (_) { /* 验证失败就当没有，继续扫码 */ }
      await page.goto('https://passport.jd.com/new/login.aspx',
                      { waitUntil: 'networkidle2', timeout: 45000 });
    }

    const QR_SEL = '#passport-main-qrcode-img';
    await page.waitForSelector(QR_SEL, { timeout: 20000 });
    // 等图片真的解码完，否则截出来是空白
    await page.waitForFunction(
      (s) => { const i = document.querySelector(s); return i && i.complete && i.naturalWidth > 50; },
      { timeout: 15000 }, QR_SEL);

    const box = await page.$eval(QR_SEL, (el) => {
      const r = el.getBoundingClientRect();
      return { x: r.x, y: r.y, width: r.width, height: r.height };
    });
    await page.screenshot({
      path: path.join(DIR, 'qr.png'),
      clip: { x: box.x, y: box.y, width: box.width, height: box.height },
    });
    writeStatus('qr');

    // 轮询 cookie：扫码确认后 pt_key 会落到 .jd.com 域
    const t0 = Date.now();
    while (Date.now() - t0 < WAIT_MS) {
      const cookies = await page.cookies(
        'https://www.jd.com', 'https://passport.jd.com', 'https://home.m.jd.com');
      const key = cookies.find((c) => c.name === 'pt_key');
      const pin = cookies.find((c) => c.name === 'pt_pin');
      if (key && pin) {
        fs.writeFileSync(path.join(DIR, 'ck.json'), JSON.stringify({
          pt_key: key.value,
          pt_pin: decodeURIComponent(pin.value),
        }));
        writeStatus('ok');
        return;
      }
      // 页面被踢回登录页说明二维码过期了
      if (await page.$('#passport-main-qrcode-img')) {
        writeStatus('qr');
      }
      await new Promise((r) => setTimeout(r, 2000));
    }
    writeStatus('expired', '二维码超时，没扫');
  } catch (e) {
    writeStatus('error', String(e.message || e).slice(0, 200));
  } finally {
    await browser.close().catch(() => {});
  }
})();
