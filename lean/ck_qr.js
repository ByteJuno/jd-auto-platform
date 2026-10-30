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
 * 二维码有效期只有 1~2 分钟，所以这里不是“截一次图然后死等”，而是分片轮转：
 * 每 QR_REFRESH_SECONDS 秒重新加载登录页换一张新码，直到 QR_SESSION_SECONDS
 * 用完或者拿到 pt_key。面板靠 status.json 里的 v（第几张）判断要不要重拉图片。
 *
 * 输出都写到 <session_dir>：
 *   qr.png     当前这张二维码（先写临时文件再 rename，避免面板读到半个文件）
 *   status.json {"state":"starting|qr|ok|expired|error","msg":"...","v":N,"ts":ms}
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
// 一整次会话最多开多久（秒），以及多久换一张新二维码（秒）。
// 京东二维码约 1~2 分钟失效，70 秒换一张，用户有充分时间扫。
const TOTAL_MS = Number(process.env.QR_SESSION_SECONDS || 600) * 1000;
const SLICE_MS = Number(process.env.QR_REFRESH_SECONDS || 70) * 1000;

// 同一个 profile 只能有一个浏览器实例，否则新实例起不来、
// 面板那边只会看到一直卡在 starting。用 pid 文件做互斥。
const LOCK = path.join(PROFILE, '..', '.ck_qr.lock');

function acquireLock() {
  const take = () => {
    const fd = fs.openSync(LOCK, 'wx');
    fs.writeSync(fd, String(process.pid));
    return fd;
  };
  try { return take(); } catch (_) { /* 已有锁 */ }
  try {
    const pid = parseInt(fs.readFileSync(LOCK, 'utf8').trim(), 10);
    if (pid && pid !== process.pid) {
      process.kill(pid, 0);          // 还活着 → 抢不到
      return null;
    }
  } catch (_) { /* 进程不在了，是残留锁 */ }
  try { fs.unlinkSync(LOCK); return take(); } catch (_) { return null; }
}


const LOGIN_URL = 'https://passport.jd.com/new/login.aspx';
const QR_SEL = '#passport-main-qrcode-img';
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
           '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36';

let version = 0;

function writeStatus(state, msg = '') {
  const tmp = path.join(DIR, '.status.json.tmp');
  fs.writeFileSync(tmp, JSON.stringify({ state, msg, v: version, ts: Date.now() }));
  fs.renameSync(tmp, path.join(DIR, 'status.json'));
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function grabQr(page) {
  await page.waitForSelector(QR_SEL, { timeout: 25000 });
  // 等图片真的解码完，否则截出来是空白
  await page.waitForFunction(
    (s) => { const i = document.querySelector(s); return i && i.complete && i.naturalWidth > 50; },
    { timeout: 20000 }, QR_SEL);
  const box = await page.$eval(QR_SEL, (el) => {
    const r = el.getBoundingClientRect();
    return { x: r.x, y: r.y, width: r.width, height: r.height };
  });
  // 二维码画布和中间的京东狗 logo 是两张图（logo 在 .qrcode-jd-logo-warp 里），
  // 画布先好、logo 后到，不等会截到没有 logo 的半成品
  await page.waitForFunction(() => {
    const i = document.querySelector('.qrcode-jd-logo-warp img');
    return i && i.complete && i.naturalWidth > 0;
  }, { timeout: 4000 }).catch(() => {});
  await sleep(200);
  const buf = await page.screenshot({
    clip: { x: box.x, y: box.y, width: box.width, height: box.height },
  });
  const tmp = path.join(DIR, '.qr.tmp.png');
  fs.writeFileSync(tmp, buf);
  fs.renameSync(tmp, path.join(DIR, 'qr.png'));
}

async function readCk(page) {
  const cookies = await page.cookies(
    'https://www.jd.com', 'https://passport.jd.com', 'https://home.m.jd.com');
  const key = cookies.find((c) => c.name === 'pt_key');
  const pin = cookies.find((c) => c.name === 'pt_pin');
  return key && pin ? { pt_key: key.value, pt_pin: decodeURIComponent(pin.value) } : null;
}

// 页面自己会说“二维码已失效”，别看时间猜
async function qrExpired(page) {
  try {
    return await page.evaluate(() =>
      /二维码已失效|二维码失效|已过期/.test(document.body.innerText));
  } catch (_) { return false; }
}

(async () => {
  // 一个 profile 只允许一个实例，否则第二个实例起不来、面板只会卡在 starting
  if (acquireLock() === null) {
    writeStatus('error', '已经有一个扫码会话在跑（浏览器 profile 被占用），先关掉那个');
    return;
  }
  process.on('exit', () => { try { fs.unlinkSync(LOCK); } catch (_) {} });

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
    await page.goto(LOGIN_URL, { waitUntil: 'networkidle2', timeout: 45000 });

    // 固定 profile 里可能已经有上一次的登录态。先验证它还有效——
    // 有效就直接复用，不产生新的登录事件（对账号最安全的一种“续期”）。
    const pre = await readCk(page);
    if (pre) {
      try {
        await page.goto('https://me-api.jd.com/user_new/info/GetJDUserInfoUnion',
                        { waitUntil: 'domcontentloaded', timeout: 20000 });
        const body = await page.evaluate(() => document.body.innerText);
        if (/"retcode"\s*:\s*"?0"?/.test(body)) {
          fs.writeFileSync(path.join(DIR, 'ck.json'),
            JSON.stringify({ ...pre, reused: true }));
          writeStatus('ok', '复用已有会话，这次不用扫');
          return;
        }
      } catch (_) { /* 验证失败就当没有，继续扫码 */ }
    }

    const deadline = Date.now() + TOTAL_MS;
    while (Date.now() < deadline) {
      await page.goto(LOGIN_URL, { waitUntil: 'networkidle2', timeout: 45000 });
      await grabQr(page);
      version += 1;
      writeStatus('qr', `第 ${version} 张，约 2 分钟内有效`);

      const sliceEnd = Math.min(Date.now() + SLICE_MS, deadline);
      while (Date.now() < sliceEnd) {
        const ck = await readCk(page);
        if (ck) {
          fs.writeFileSync(path.join(DIR, 'ck.json'), JSON.stringify(ck));
          writeStatus('ok', '扫码成功');
          return;
        }
        if (await qrExpired(page)) break;   // 京东自己说过期了，立刻换下一张
        await sleep(1200);
      }
    }
    writeStatus('expired', '一直没扫到，已停止；再点一次「添加账号」换新码');
  } catch (e) {
    writeStatus('error', String(e.message || e).slice(0, 200));
  } finally {
    await browser.close().catch(() => {});
  }
})();
