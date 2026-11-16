// 后台自动导出（v1.8）——你不用点按钮，登录完文件自己就有。
//
// v1.5 修的真 bug：URL.createObjectURL 在 Service Worker 里**不存在**，只在页面里才有。
// v1.4 直接调它，于是每次都报 "URL.createObjectURL is not a function"，导出永远失败。
// 现在改成：让同源的隐藏页面 offscreen.html 造 blob 地址（官方给 Service Worker 的
// 标准解法），实在不行再退到纯 data: 地址。
//
// v1.4 的另一处改动保留：不依赖 chrome.storage（这台机器上它的 LevelDB 写不进去），
// 存储能用就用、坏了也照导。
//
// v1.7 修的三个真问题：
//   1) 浏览器会随时把后台脚本冻掉以省内存，冻掉后内存里的"上次导出时间"就清零了。
//      醒来后它以为自己从没导过，于是又导一份明文 —— 结果只要正常上网，明文登录票
//      会被一遍遍往下载夹里写。现在改成从"下载记录"里恢复时间（记录在磁盘上，
//      重启还在），已经导过就不再重复导。
//   2) 以前的域名判断写得太松（结尾包含就算），notbilibili.com 这种假站也会被当成
//      B站。现在只认"完全相同"或"以 .域名 结尾"。
//   3) 每 15 分钟的兜底闹钟，以前每次后台醒来都会重设一遍，等于永远往后推，
//      实际可能一次都不响。现在只在没有时才创建。
//
// v1.8 修的三件事（都是"会重复写明文"或"该导没导"的毛病）：
//   4) 光恢复"上次时间"不够：上次的 cookie 指纹也在内存里，冻掉就没了。于是每 15
//      分钟的兜底检查都会认为"cookie 变了"，又写一份。现在把指纹写进文件名
//      （mc_cookies_auto_时间_指纹.json），醒来连指纹一起恢复，真没变就不再写。
//   5) 以前任何 cookie 变化都触发导出 —— 你随便刷网页，网站也会不停更新一些无关
//      cookie，结果每 2 分钟就往下载夹写一份明文。现在只在"登录票"本身变化时才导
//      （B站的 SESSDATA、微博的 SUB、抖音的 sessionid 这类）。
//   6) 两次导出离得太近时，以前直接把这次变化丢掉，要等 15 分钟兜底才补上。现在
//      改成排一个"刚好到点"的延后导出，既不重复写，也不会漏掉刚登录的平台。
//
// 触发方式：
//   - 装好/浏览器启动时导一次；
//   - 登录票变化时，30 秒后合并导出一次；
//   - 每 15 分钟兜底检查一次（cookie 没变就不写文件）；
//   - 后台被唤起时只在"从来没导出过"的情况下补一次。

const PLATFORMS = [
  { key: 'bili',  domains: ['bilibili.com'] },
  { key: 'wb',    domains: ['weibo.com', 'weibo.cn', 'sina.com.cn'] },
  { key: 'dy',    domains: ['douyin.com'] },
  { key: 'ks',    domains: ['kuaishou.com'] },
  { key: 'tieba', domains: ['baidu.com'] },
  { key: 'xhs',   domains: ['xiaohongshu.com'] },
  { key: 'zhihu', domains: ['zhihu.com'] }
];

// 只有这些"登录票"变化才值得导出。网站浏览时会不停刷新一堆无关 cookie
// （埋点、uuid、ab 分组…），跟着它们导等于每两分钟写一份明文，没必要。
const LOGIN_COOKIES = {
  bili:  ['SESSDATA', 'bili_jct', 'DedeUserID'],
  wb:    ['SUB', 'SUBP', 'SSOLoginState'],
  dy:    ['sessionid', 'sessionid_ss', 'sid_tt', 'passport_csrf_token'],
  ks:    ['kuaishou.server.web_st', 'kuaishou.server.webday7_st', 'kuaishou.server.web_ph'],
  tieba: ['BDUSS', 'BDUSS_BFESS', 'STOKEN'],
  xhs:   ['web_session', 'a1'],
  zhihu: ['z_c0', 'd_c0']
};
const ALL_LOGIN_NAMES = new Set(
  Object.keys(LOGIN_COOKIES).reduce((all, k) => all.concat(LOGIN_COOKIES[k]), [])
);


const ALL_DOMAINS = PLATFORMS.flatMap(p => p.domains);
const AUTO_PREFIX = 'mc_cookies_auto_';
const MIN_GAP_MS = 2 * 60 * 1000;      // cookie 触发时，两次导出至少隔 2 分钟
const PERIODIC_MIN = 15;               // 兜底周期
const PERIODIC_MIN_GAP_MS = 10 * 60 * 1000; // 定时兜底时，至少隔 10 分钟才重复导
const KEEP_FILES = 2;

// 状态只放内存：不碰 chrome.storage 也照样工作
let lastExportAt = 0;
let lastPrint = '';
let lastReason = '';
const recent = [];   // 最近若干次执行记录，只在内存里，用于 console 排查

function note(stage, extra) {
  const line = '[mc] ' + stage + (extra ? ' ' + JSON.stringify(extra) : '');
  recent.push({ at: new Date().toLocaleTimeString('sv-SE'), stage, extra: extra || null });
  if (recent.length > 20) recent.shift();
  console.log(line);
}

function slim(c) {
  return {
    name: c.name,
    value: c.value,
    domain: c.domain,
    path: c.path,
    expirationDate: c.expirationDate || null,
    hostOnly: c.hostOnly,
    secure: c.secure,
    httpOnly: c.httpOnly,
    sameSite: c.sameSite
  };
}

function stamp() {
  const d = new Date();
  const p = n => String(n).padStart(2, '0');
  return '' + d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) +
         '_' + p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds());
}

// 指纹只算"登录票"，不算埋点/uuid 这类无关 cookie。
// 否则你在网上随便逛逛，站点改了个无关 cookie，指纹就变了，兜底检查又写一份明文。
// 某个平台一条登录票都没有时，退回去算它的全部 cookie —— 免得这个平台换了
// cookie 名字后，插件以为"永远没变化"再也不会更新。
function fingerprint(platforms) {
  let h = 2166136261;
  const feed = s => {
    for (let i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = (h * 16777619) >>> 0;
    }
  };
  for (const key of Object.keys(platforms)) {
    const list = platforms[key].cookies;
    const login = list.filter(c => ALL_LOGIN_NAMES.has(c.name));
    const use = login.length ? login : list;
    for (const c of use) feed(key + '|' + c.domain + '|' + c.name + '|' + c.value);
  }
  return h.toString(16);
}

async function badge(text, color) {
  try {
    await chrome.action.setBadgeText({ text: text || '' });
    if (color) await chrome.action.setBadgeBackgroundColor({ color });
  } catch (e) {}
}

async function grabOne(p, errs) {
  const seen = new Set();
  const out = [];
  for (const domain of p.domains) {
    let list = [];
    try {
      list = await chrome.cookies.getAll({ domain });
    } catch (e) {
      errs.push(p.key + '/' + domain + ': ' + (e && e.message ? e.message : String(e)));
      continue;
    }
    for (const c of list) {
      const id = c.domain + '|' + c.path + '|' + c.name;
      if (seen.has(id)) continue;
      seen.add(id);
      out.push(slim(c));
    }
  }
  return out;
}

// 下载记录里的 startTime 是"年-月-日 时:分:秒"这种文字，不是数字。
// 直接 a - b 算出来是无效值，排序等于没排 —— 于是"只留最近两份"可能删掉最新的、
// 留下最老的。这里统一换成毫秒数再比。
function ms(t) {
  const v = Date.parse(t || '');
  return isNaN(v) ? 0 : v;
}

async function pruneOldFiles() {
  try {
    const items = await chrome.downloads.search({ filenameRegex: 'mc_cookies_auto_.*\\.json$' });
    // 正在下的那份也要算进"保留"，否则刚下完时判断成"已完成的最老两份"，
    // 结果磁盘上多留一份明文（老逻辑实测会剩 3 份）。
    const keepable = (items || [])
      .filter(i => i.state === 'complete' || i.state === 'in_progress')
      .sort((a, b) => ms(b.startTime) - ms(a.startTime));
    for (const it of keepable.slice(KEEP_FILES)) {
      try { await chrome.downloads.removeFile(it.id); } catch (e) {}
      try { await chrome.downloads.erase({ id: it.id }); } catch (e) {}
    }
  } catch (e) {}
}

// 后台被冻过之后内存全丢（lastExportAt=0），醒来会误以为"从没导过"再导一份明文。
// 下载记录是落在磁盘上的，重启也在，所以用它的时间戳把内存状态补回来。
// 这里认所有 mc_cookies_*.json（含弹窗里手动导的那份）：手动导过也算"刚导过"，
// 免得后台过一会儿又补一份一模一样的。
//
// v1.8：文件名里带着 cookie 指纹（mc_cookies_auto_时间_fp指纹.json），
// 醒来时把它一起捡回来，这样"cookie 没变"这个判断在冻过之后仍然成立。
// 返回 { ever, print }
async function restoreStateFromDownloads() {
  try {
    const items = await chrome.downloads.search({ filenameRegex: 'mc_cookies_.*\\.json$' });
    const done = (items || [])
      .filter(i => i.state === 'complete' || i.state === 'in_progress')
      .sort((a, b) => ms(b.startTime) - ms(a.startTime));
    if (!done.length) return { ever: false, print: '' };
    const newest = done[0];
    const t = Date.parse(newest.startTime || '') || 0;
    if (t > lastExportAt) lastExportAt = t;
    // 指纹不是从"最新那份"里找，而是从"最新的带指纹那份"里找。
    // 因为弹窗里手动导的文件名没有指纹，如果它恰好是最新的，只看最新那份就找不到了，
    // 结果会白白多导一次。老版本的文件名也没有 _fp，认不出就当没恢复。
    if (!lastPrint) {
      const withFp = done.find(i => /_fp([0-9a-f]{1,16})\.json$/.test(i.filename || ''));
      const m = withFp && /_fp([0-9a-f]{1,16})\.json$/.exec(withFp.filename);
      if (m) lastPrint = m[1];
    }
    return { ever: true, print: lastPrint };
  } catch (e) {
    return { ever: false, print: '' };
  }
}

// 存储坏了就跳过，绝不让它挡住导出
async function withoutStorage(fn) {
  try {
    if (!chrome.storage || !chrome.storage.local) return null;
    return await fn(chrome.storage.local);
  } catch (e) {
    return null;
  }
}

// 后台（Service Worker）里没有 URL.createObjectURL，必须借隐藏页面来造下载地址。
// 返回 { url, via, revoke }
//
// v1.7 注意：这里的 revoke 不能直接调 URL.revokeObjectURL —— 后台环境里没有这个函数，
// 以前那样写等于永远不释放。现在改成让隐藏页面自己撤销（发消息过去），
// 并且下载完成后把隐藏页面关掉，不留着占内存。
async function makeDownloadUrl(text) {
  // 首选：离屏页面造 blob 地址
  try {
    if (chrome.offscreen && chrome.offscreen.createDocument) {
      try {
        await chrome.offscreen.createDocument({
          url: 'offscreen.html',
          reasons: ['BLOBS'],
          justification: '导出登录票 JSON 需要生成 blob 下载地址'
        });
      } catch (e) {
        // 已经开着会报错，忽略
      }
      const resp = await chrome.runtime.sendMessage({ type: 'make-blob-url', text });
      if (resp && resp.url) {
        return {
          url: resp.url,
          via: 'offscreen',
          revoke: () => {
            try { chrome.runtime.sendMessage({ type: 'revoke-blob-url', url: resp.url }).catch(() => {}); }
            catch (e) {}
          }
        };
      }
      note('离屏页面没给出地址', { resp: resp || null });
    } else {
      note('这个浏览器没有 chrome.offscreen');
    }
  } catch (e) {
    note('离屏页面失败', { error: (e && e.message) ? e.message : String(e) });
  }

  // 兜底：纯文本 data 地址，不需要任何页面
  return {
    url: 'data:application/json;charset=utf-8,' + encodeURIComponent(text),
    via: 'data-url',
    revoke: null
  };
}

// 等下载真正结束（成功或失败）再撤销地址、关掉隐藏页面。
// 不能一下载就关：blob 地址是藏在那个隐藏页里的，页面一关地址就失效，下载会失败。
function cleanupAfterDownload(downloadId, made) {
  let finished = false;
  const onCh = delta => {
    if (!delta || delta.id !== downloadId) return;
    if (delta.state && (delta.state.current === 'complete' || delta.state.current === 'interrupted')) {
      finish();
    }
  };
  const finish = () => {
    if (finished) return;
    finished = true;
    try { chrome.downloads.onChanged.removeListener(onCh); } catch (e) {}
    try { if (made.revoke) made.revoke(); } catch (e) {}
    setTimeout(() => {
      try { chrome.offscreen.closeDocument().catch(() => {}); } catch (e) {}
    }, 3000);
  };
  try { chrome.downloads.onChanged.addListener(onCh); } catch (e) {}
  // 兜底：60 秒还没动静就强制清理，别一直挂着
  setTimeout(finish, 60000);
}

let busy = false;

async function exportAll(reason, force) {
  // 启动/闹钟/cookie 变化可能在同一秒一起触发，锁住，避免一次重载导出两份明文
  if (busy) {
    note('skip: 上一次导出还没跑完', { reason });
    return { skipped: 'busy' };
  }
  busy = true;
  try {
    return await exportAllUnlocked(reason, !!force);
  } finally {
    busy = false;
  }
}

async function exportAllUnlocked(reason, force) {
  try {
    const now = Date.now();
    // 手动强制导出时不能拿"上次导出时间"去挡自己（否则点了按钮没反应）
    if (!force) {
      // 内存里的"上次导出时间"可能是空的（后台被冻过）。先看磁盘上的下载记录补回来。
      if (!lastExportAt || !lastPrint) await restoreStateFromDownloads();
      const gap = reason === '定时兜底' ? PERIODIC_MIN_GAP_MS : MIN_GAP_MS;
      if (lastExportAt && now - lastExportAt < gap) {
        // 不是丢掉这次变化，而是排一个"刚好到点"的延后导出。
        // 以前直接 skip，如果用户这时刚登录完某个平台，得等 15 分钟兜底才补上。
        const waitMs = gap - (now - lastExportAt) + 1000;
        note('延后导出: 离上次太近', { reason, waitSec: Math.round(waitMs / 1000) });
        try {
          chrome.alarms.create('auto-export-now', { delayInMinutes: waitMs / 60000 });
        } catch (e) {}
        return { skipped: 'too soon' };
      }
    }

    note('开始导出', { reason });

    const platforms = {};
    const errs = [];
    let total = 0;
    for (const p of PLATFORMS) {
      const cookies = await grabOne(p, errs);
      platforms[p.key] = { domains: p.domains, cookies };
      total += cookies.length;
    }
    note('读完 cookie', { total, errs });

    if (total === 0) {
      await badge('0', '#d93025');
      note('失败: 一条 cookie 都没读到', { errs });
      return { skipped: 'no cookies' };
    }

    const print = fingerprint(platforms);
    // cookie 和上次导的一模一样 → 不写文件，不管隔了多久。
    // 以前这里还加了"10 分钟内"这个条件，而兜底闹钟每 15 分钟就响一次，
    // 等于每 15 分钟必然又写一份明文 —— 时间条件正好帮它开了后门。
    // 只有登录票真的变了才值得产生新文件（手动强制导出不受这条限制）。
    if (!force && print === lastPrint) {
      note('skip: cookie 没变化');
      return { skipped: 'unchanged' };
    }

    const payload = {
      exported_at: new Date().toLocaleString('sv-SE'),
      source: 'edge-extension-auto',
      version: 1,
      platforms
    };
    // 指纹放进文件名：后台被冻过后内存清零，靠它把"上次导的是哪份 cookie"找回来
    const filename = AUTO_PREFIX + stamp() + '_fp' + print + '.json';
    const text = JSON.stringify(payload, null, 2);
    const made = await makeDownloadUrl(text);
    note('拿到下载地址', { via: made.via });

    let downloadId = null;
    try {
      downloadId = await chrome.downloads.download({ url: made.url, filename, saveAs: false });
    } catch (e) {
      // 下载没起来，立刻回收地址和隐藏页面
      try { if (made.revoke) made.revoke(); } catch (e2) {}
      try { chrome.offscreen.closeDocument().catch(() => {}); } catch (e2) {}
      throw e;
    }
    // 下载成功起步后，等它真正落地再回收（见 cleanupAfterDownload 注释）
    if (downloadId != null) cleanupAfterDownload(downloadId, made);

    lastExportAt = now;
    lastPrint = print;
    lastReason = reason;
    await badge(String(total), '#188038');
    setTimeout(() => { badge(''); }, 15000);
    note('导出成功', { file: filename, total, downloadId });
    await pruneOldFiles();
    return { file: filename, total };
  } catch (e) {
    const msg = (e && e.message) ? e.message : String(e);
    console.error('[mc] 导出失败: ' + msg + '\n' + ((e && e.stack) ? e.stack : ''));
    note('失败', { error: msg, reason });
    try { await badge('!', '#d93025'); } catch (e2) {}
    return { error: msg };
  }
}

function onCookieDomain(domain) {
  const d = (domain || '').replace(/^\./, '').toLowerCase();
  // 只认"完全一样"或"是它的子域名"。以前多写了一个 d.endsWith(x)，
  // 会让 notbilibili.com 这种假域名也命中。
  return ALL_DOMAINS.some(x => d === x || d.endsWith('.' + x));
}

/* ---------- 下面这段也是"锦上添花"，存储坏了不影响主流程 ---------- */

// 兜底闹钟只在"还没有这个闹钟"时才创建。以前每次后台醒来都 create 一遍，
// 等于每次都把 15 分钟往后推，实际可能永远不响。
function ensurePeriodicAlarm() {
  try {
    chrome.alarms.get('auto-export-periodic').then(a => {
      if (!a) {
        chrome.alarms.create('auto-export-periodic', { periodInMinutes: PERIODIC_MIN });
      }
    }).catch(() => {});
  } catch (e) {}
}

note('后台脚本启动 1.8');
withoutStorage(s => s.set({ boot: new Date().toLocaleString('sv-SE'), bootVer: '1.8' }));
note('chrome.storage 可用性', { storage: !!(chrome.storage && chrome.storage.local) });

chrome.cookies.onChanged.addListener(info => {
  if (!info || !info.cookie) return;
  if (!onCookieDomain(info.cookie.domain)) return;
  // 只盯登录票。以前任何 cookie 变化都触发导出 —— 你随便刷网页，站点会不停
  // 更新埋点/uuid 这类无关 cookie，结果每 2 分钟就往下载夹写一份明文。
  if (!ALL_LOGIN_NAMES.has(info.cookie.name)) return;
  note('登录票有变化', { name: info.cookie.name, domain: info.cookie.domain, removed: !!info.removed });
  try {
    chrome.alarms.get('auto-export-now').then(a => {
      if (!a) return chrome.alarms.create('auto-export-now', { delayInMinutes: 0.5 });
    }).catch(() => {});
  } catch (e) {}
});

chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === 'auto-export-now') {
    exportAll('cookie 有变化');
  } else if (alarm.name === 'auto-export-periodic') {
    exportAll('定时兜底');
  }
});

try {
  chrome.runtime.onInstalled.addListener(() => {
    ensurePeriodicAlarm();
    exportAll('首次安装');
  });
  chrome.runtime.onStartup.addListener(() => {
    ensurePeriodicAlarm();
    exportAll('浏览器启动');
  });
} catch (e) {}

ensurePeriodicAlarm();

// 手动点按钮时走这里：无视"太近/没变化"的限制，强制导一份。
// v1.7 注意：不能靠"把 lastExportAt 清零"来绕过检查 —— 清零后导出流程又会从
// 下载记录把时间补回来，结果按钮点了没反应。现在改成显式传 force。
globalThis.__mcForceExport = () => exportAll('手动强制', true);

// 后台被唤起（含点"重新加载"）时补导一次 —— 但只有"从来没导出过"才补。
// 以前这里无条件 exportAll，配合内存清零，就变成每次醒来都往下载夹塞一份明文。
(async () => {
  const st = await restoreStateFromDownloads();
  if (st.ever) {
    note('后台启动: 以前导出过，不重复导', { lastExportAgo: Math.round((Date.now() - lastExportAt) / 1000) });
    return;
  }
  exportAll('后台启动');
})();
