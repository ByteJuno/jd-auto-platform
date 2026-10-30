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
// 京东二维码约 2 分钟失效，100 秒换一张；旧码所在页面不关，等于同时有两张活码。
const TOTAL_MS = Number(process.env.QR_SESSION_SECONDS || 600) * 1000;
const SLICE_MS = Number(process.env.QR_REFRESH_SECONDS || 100) * 1000;

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

// 同一个浏览器上下文，任何活着的页面都能读到同一份 cookie；
// 从最新往回找，避免刚好在切换页面时漏掉。
async function readCk(pages) {
  for (const p of pages.slice().reverse()) {
    if (!p || p.isClosed()) continue;
    try {
      const cs = await p.cookies(
        'https://www.jd.com', 'https://passport.jd.com', 'https://home.m.jd.com');
      const key = cs.find((c) => c.name === 'pt_key');
      const pin = cs.find((c) => c.name === 'pt_pin');
      if (key && pin) return { pt_key: key.value, pt_pin: decodeURIComponent(pin.value) };
    } catch (_) { /* 页面刚被关掉，换上一个 */ }
  }
  return null;
}

async function pageText(page) {
  try { return await page.evaluate(() => document.body.innerText); } catch (_) { return ''; }
}

// 拿到 pt_key 不等于能用，打一次 me-api 看 retcode 才算数
async function validCk(browser, ck) {
  const p = await browser.newPage();
  try {
    await p.setUserAgent(UA);
    await p.goto('https://me-api.jd.com/user_new/info/GetJDUserInfoUnion',
                 { waitUntil: 'domcontentloaded', timeout: 20000 });
    const t = await pageText(p);
    return /"retcode"\s*:\s*"?0"?/.test(t);
  } catch (_) {
    return false;
  } finally {
    try { await p.close(); } catch (_) {}
  }
}

async function openLoginPage(browser) {
  const page = await browser.newPage();
  await page.setUserAgent(UA);
  await page.setViewport({ width: 460, height: 820 });
  try {
    await page.goto(LOGIN_URL, { waitUntil: 'networkidle2', timeout: 45000 });
  } catch (e) {
    try { await page.close(); } catch (_) {}
    throw e;
  }
  return page;
}

// 家宽 DNS 偶尔抽一下（ERR_NAME_NOT_RESOLVED），换码失败不该把整场会话搞死
async function openLoginPageRetry(browser, tries = 3) {
  let last;
  for (let i = 0; i < tries; i++) {
    try { return await openLoginPage(browser); }
    catch (e) { last = e; await sleep(1500 * (i + 1)); }
  }
  throw last;
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

  // 关键点：每张二维码都活在自己那个页面里。换码时**不销毁旧页面**，
  // 否则用户扫的正是旧页面那张码时，京东那边会显示“登录成功”，
  // 但票没人来收（收票的 JS 随页面一起没了），cookie 永远写不进来。
  const pages = [];
  try {
    const first = await openLoginPage(browser);
    pages.push(first);

    // 固定 profile 里可能已经有上一次的登录态。先验证它还有效——
    // 有效就直接复用，不产生新的登录事件（对账号最安全的一种“续期”）。
    const pre = await readCk(pages);
    if (pre && await validCk(browser, pre)) {
      fs.writeFileSync(path.join(DIR, 'ck.json'), JSON.stringify({ ...pre, reused: true }));
      writeStatus('ok', '复用已有会话，这次不用扫');
      return;
    }

    await grabQr(first);
    version += 1;
    writeStatus('qr', `第 ${version} 张，约 2 分钟内有效`);

    const deadline = Date.now() + TOTAL_MS;
    let sliceEnd = Math.min(Date.now() + SLICE_MS, deadline);

    while (Date.now() < deadline) {
      const ck = await readCk(pages);
      if (ck) {
        fs.writeFileSync(path.join(DIR, 'ck.json'), JSON.stringify(ck));
        writeStatus('ok', '扫码成功');
        return;
      }

      // 页面会告诉你“扫描成功，请在手机上确认”，把它透到面板上
      const t = await pageText(pages[pages.length - 1]);
      if (/扫描成功|请在手机|已扫码/.test(t)) {
        writeStatus('qr', '已扫码，请在手机上点「确认登录」');
      }

      if (Date.now() >= sliceEnd) {
        let np = null;
        try {
          np = await openLoginPageRetry(browser);
          await grabQr(np);
        } catch (e) {
          if (np) { try { await np.close(); } catch (_) {} }
          np = null;
          // 换码失败不退场：手里那张还在，30 秒后再试
          writeStatus('qr', `第 ${version} 张（换新码失败，继续用这张）：` +
                            String(e.message || e).slice(0, 60));
        }
        if (np) {
          pages.push(np);
          version += 1;
          writeStatus('qr', `第 ${version} 张，约 2 分钟内有效`);
          // 只留最近两张，别一次开一堆登录会话（风控/资源都受不了）
          while (pages.length > 2) {
            const old = pages.shift();
            try { await old.close(); } catch (_) {}
          }
        }
        sliceEnd = Date.now() + (np ? SLICE_MS : 30000);
        sliceEnd = Math.min(sliceEnd, deadline);
      }
      await sleep(1000);
    }
    writeStatus('expired', '一直没扫到，已停止；再点一次「添加账号」换新码');
  } catch (e) {
    writeStatus('error', String(e.message || e).slice(0, 200));
  } finally {
    await browser.close().catch(() => {});
  }
})();