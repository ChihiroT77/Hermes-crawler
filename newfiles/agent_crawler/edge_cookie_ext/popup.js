// 点一下就把本机 Edge 里这几个平台的 cookie 导出成 JSON。
// 原理：扩展在浏览器内部调用官方接口 chrome.cookies.getAll，
// 浏览器直接给明文，所以不用解密、也不受新版加密的影响。

// 每个平台要抓的域名（getAll 会连子域名一起给）
const PLATFORMS = [
  { key: 'bili',  label: 'B站',     domains: ['bilibili.com'] },
  { key: 'wb',    label: '微博',    domains: ['weibo.com', 'weibo.cn', 'sina.com.cn'] },
  { key: 'dy',    label: '抖音',    domains: ['douyin.com'] },
  { key: 'ks',    label: '快手',    domains: ['kuaishou.com'] },
  { key: 'tieba', label: '贴吧',    domains: ['baidu.com'] },
  { key: 'xhs',   label: '小红书（默认不导入）', domains: ['xiaohongshu.com'] },
  { key: 'zhihu', label: '知乎（默认不导入）',   domains: ['zhihu.com'] }
];

// 只留爬虫真正用得上的字段，别把无关信息带走
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

async function grabOne(p) {
  const seen = new Set();
  const out = [];
  for (const domain of p.domains) {
    let list = [];
    try {
      list = await chrome.cookies.getAll({ domain });
    } catch (e) {
      continue; // 某些域名没权限或没 cookie，跳过就行
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

function stamp() {
  const d = new Date();
  const p = n => String(n).padStart(2, '0');
  return '' + d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) +
         '_' + p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds());
}

async function downloadJson(text, filename) {
  const blob = new Blob([text], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  try {
    await chrome.downloads.download({ url, filename, saveAs: false });
  } finally {
    // 立刻回收这段内存地址，别留着
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  }
}

const logEl = document.getElementById('log');
const autoEl = document.getElementById('auto');

function say(lines) {
  logEl.textContent = lines.join('\n');
}

// 下载记录里的时间是文字（如 2026-11-15 14:18），直接相减得不出结果、排序等于没排，
// 于是"最近一次"可能显示成很旧的那份。统一换成毫秒数再比。
function ms(t) {
  const v = Date.parse(t || '');
  return isNaN(v) ? 0 : v;
}

// 显示"后台自动导出"的状态：上次什么时候导的、有几个文件。
// 后台是静默工作的，用户看不到，这里给他一个能确认的入口。
async function showAutoStatus() {
  try {
    const items = await chrome.downloads.search({ filenameRegex: 'mc_cookies_auto_.*\\.json$' });
    const done = (items || [])
      .filter(i => i.state === 'complete' || i.state === 'in_progress')
      .sort((a, b) => ms(b.startTime) - ms(a.startTime));
    if (!done.length) {
      autoEl.className = 'warn';
      autoEl.textContent = '自动导出：还没导过。打开任意一个平台网站并确认已登录，插件会自动导一份；'
        + '也可以点下面的按钮手动导。';
      return;
    }
    const t = Date.parse(done[0].startTime || '');
    const when = t ? new Date(t).toLocaleString('zh-CN', { hour12: false }) : '时间不明';
    autoEl.className = '';
    autoEl.textContent = '自动导出：工作中，最近一次 ' + when + '。'
      + '（浏览器后台会盯着登录状态，你在别的网页登录后会自动补一份）';
  } catch (e) {
    autoEl.className = 'warn';
    autoEl.textContent = '自动导出：读不到状态（' + (e && e.message ? e.message : String(e)) + '）';
  }
}

showAutoStatus();

// 导出指定平台；keys 为空表示全部
async function exportPlatforms(keys) {
  const picked = PLATFORMS.filter(p => keys.length === 0 || keys.includes(p.key));
  say(['正在读 cookie…']);

  const platforms = {};
  const lines = [];
  for (const p of picked) {
    const cookies = await grabOne(p);
    platforms[p.key] = { domains: p.domains, cookies };
    lines.push(`${p.label}：${cookies.length} 条${cookies.length ? '' : '（这个浏览器里没登录或没数据）'}`);
  }

  const payload = {
    exported_at: new Date().toLocaleString('sv-SE'), // 形如 2026-11-13 22:20:31
    source: 'edge-extension',
    version: 1,
    platforms
  };

  const only = keys.length === 1 ? keys[0] + '_' : '';
  const filename = 'mc_cookies_' + only + stamp() + '.json';
  try {
    await downloadJson(JSON.stringify(payload, null, 2), filename);
    lines.push('', '已导出：' + filename, '文件在"下载"文件夹里。');
    // 手动导完，后台看到下载记录就不会再多导一份一模一样的
    if (typeof showAutoStatus === 'function') setTimeout(showAutoStatus, 800);
  } catch (e) {
    lines.push('', '导出失败：' + e.message);
  }
  say(lines);
}

function addButton(text, keys, cls) {
  const b = document.createElement('button');
  b.textContent = text;
  if (cls) b.className = cls;
  b.addEventListener('click', () => exportPlatforms(keys));
  document.getElementById('buttons').appendChild(b);
}

addButton('全部导出（推荐）', [], 'all');
for (const p of PLATFORMS) {
  addButton('只导出 ' + p.label, [p.key]);
}
