/**
 * dsh-hermes 宿主半身（host half）。
 *
 * 这一层只做一件事：把 dsh 的工具调用，翻译成对 agent_crawl.py 的一次进程调用。
 * 爬虫的全部逻辑都在 agent_crawl.py 里（那一层没被本文件改动一个字）。
 *
 * 设计上的三条硬规矩：
 *   1. 不重写业务逻辑 —— 外壳只负责"拼命令行 + 收 JSON"，判断"能不能爬"交给内核。
 *   2. 不让插件崩掉会话 —— 内核没装好时，工具返回一句人话，而不是抛异常。
 *   3. 一次调用只干一件事 —— 长任务交给内核的后台模式，再用 status 去看，
 *      免得 AI 卡在"到底跑完没有"上。
 *
 * @module dsh-hermes
 */

import { execFile } from 'node:child_process'
import { existsSync, mkdirSync, writeFileSync } from 'node:fs'
import { homedir, tmpdir } from 'node:os'
import { join } from 'node:path'

export const name = 'dsh-hermes'

/** 只需要工具注册表；技能、数据库一概不碰。 */
export const inject = ['tools']

/* -------------------------------------------------------------------------- */
/* 各种超时（毫秒）                                                             */
/* -------------------------------------------------------------------------- */
/* 为什么每个工具不一样：查版本是读本地常量，秒回；查状态要跑一次 PowerShell 看进程，
 * 慢一点；真爬取可能几十分钟，只能靠内核的后台模式，前台等 15 秒拿到"已启动"就走。 */
const TIMEOUT = {
  version: 30_000,
  defaults: 30_000,
  status: 90_000,
  loginCheck: 120_000,
  estimate: 90_000,
  cancel: 150_000,
  export: 300_000,
  /** 后台启动这条路：内核最多盯 12 秒就会回话，留足余量。 */
  detach: 120_000,
  /** 前台真跑：到点就掐，避免一个会话被一个长任务拖死。 */
  foreground: 30 * 60_000,
}

/**
 * 解析爬虫装在哪、用哪个 Python。
 *
 * 顺序：配置里的 pythonPath 优先 → 其次 <crawlerRoot>/.venv 里的。
 * 之所以要分层，是因为用户可能装在别处、也可能故意用别的解释器跑。
 *
 * @param {{ crawlerRoot?: string, pythonPath?: string }} [opts] cordis 传进来的配置
 * @returns {{ crawlerRoot: string, pythonPath: string, scriptPath: string }}
 */
function resolvePaths(opts) {
  const crawlerRoot =
    typeof opts?.crawlerRoot === 'string' && opts.crawlerRoot.trim() !== ''
      ? opts.crawlerRoot.trim()
      : join(homedir(), 'MediaCrawler')

  const venvPython = join(crawlerRoot, '.venv', 'Scripts', 'python.exe')
  const pythonPath =
    typeof opts?.pythonPath === 'string' && opts.pythonPath.trim() !== ''
      ? opts.pythonPath.trim()
      : venvPython

  return {
    crawlerRoot,
    pythonPath,
    scriptPath: join(crawlerRoot, 'agent_crawler', 'agent_crawl.py'),
  }
}

/**
 * 跑一次内核命令，把 stdout 当作 JSON 解析。
 *
 * 内核约定：--json 模式下永远往 stdout 打一段 JSON，退出码不保证为 0（"爬失败"是
 * 正常业务结果，不是程序崩溃）。所以这里不按退出码判成败，只按"能不能解析出 JSON"判。
 *
 * @param {string} pythonPath 解释器
 * @param {string[]} args 传给脚本的参数
 * @param {number} timeoutMs 超时
 * @param {AbortSignal} [signal] 来自 dsh 的中止信号，透传给子进程
 * @returns {Promise<{ json: object|null, stderr: string, code: number, timedOut: boolean }>}
 */
function runJson(pythonPath, args, timeoutMs, signal) {
  return new Promise((resolve, reject) => {
    execFile(
      pythonPath,
      args,
      { timeout: timeoutMs, windowsHide: true, maxBuffer: 32 * 1024 * 1024, signal },
      (error, stdout, stderr) => {
        if (error?.name === 'AbortError') {
          // 用户按了停止：这是"被叫停"，不是"执行失败"，要原样抛上去
          reject(error)
          return
        }
        let json = null
        const text = String(stdout ?? '').trim()
        if (text !== '') {
          try {
            json = JSON.parse(text)
          } catch {
            // 不是 JSON（比如内核被别的东西污染了输出）——交给调用方按"读不懂"处理
            json = null
          }
        }
        // execFile 超时时也会进这里（error.killed），要单独认出来，否则会被说成"退出码 1"
        const timedOut = error?.killed === true && error?.signal != null
        const code = typeof error?.code === 'number' ? error.code : error ? 1 : 0
        resolve({ json, stderr: String(stderr ?? ''), code, timedOut })
      },
    )
  })
}

/** 内核没装好时给人看的一句话（区分"路径不存在"和"跑不起来"）。 */
function notInstalledHint(paths, stderr) {
  if (!existsSync(paths.crawlerRoot)) {
    return `MediaCrawler 还没装（找不到 ${paths.crawlerRoot}）。先跑 install.ps1 装好，再来用。`
  }
  if (!existsSync(paths.scriptPath)) {
    return `MediaCrawler 在 ${paths.crawlerRoot}，但里面没有 agent_crawler/agent_crawl.py。安装可能没走完，重跑 install.ps1。`
  }
  if (!existsSync(paths.pythonPath)) {
    return `找不到 Python 解释器（${paths.pythonPath}）。装的时候建 .venv 这一步可能失败了，重跑 install.ps1。`
  }
  return `内核跑不起来：${stderr.trim().split('\n').slice(-3).join(' ') || '没有输出'}`
}

/* -------------------------------------------------------------------------- */
/* 拼命令行的小工具                                                             */
/* -------------------------------------------------------------------------- */
/* 内核的参数风格是 --flag / --flag 值 / --flag 与 --no-flag 成对。这里只用三个小函数
 * 覆盖，不做通用转换器——通用转换器看着优雅，但"哪些是布尔对、哪些是数字"还得另写
 * 一张表，反而更难核对。 */

/** 有值才加：空串、null、undefined 一律当"用户没提"，交给内核用默认值。 */
function pushValue(args, flag, value) {
  if (value === undefined || value === null) return
  const s = typeof value === 'string' ? value.trim() : value
  if (s === '') return
  args.push(flag, String(s))
}

/** 数字才加：0 是有意义的（比如 retries=0），所以不能用真假判断。 */
function pushNumber(args, flag, value) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return
  args.push(flag, String(Math.trunc(value)))
}

/** 小数也保留（--wait-lock 允许 0.5 分钟这种值，截断会把 0.5 变成 0）。 */
function pushDecimal(args, flag, value) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return
  args.push(flag, String(value))
}

/** 布尔对：true 给正flag、false 给负flag、没给就都不加。 */
function pushToggle(args, positive, negative, value) {
  if (value === true) args.push(positive)
  else if (value === false) args.push(negative)
}

/** 从内核返回里取一句能给人看的原因（内核字段名统一叫 reason）。 */
function reasonOf(json) {
  return typeof json?.reason === 'string' ? json.reason : ''
}

/** 取一句短状态（内核字段名统一叫 status）。 */
function statusOf(json) {
  return typeof json?.status === 'string' ? json.status : ''
}

/**
 * 把一次内核调用包成统一的工具返回。
 *
 * 统一形状的意义：模型只要认一次（ok / status / summary / error / detail），
 * 10 个工具都一样，不会出现"这个工具能读、那个工具读不懂"。
 *
 * @param {object|null} json 内核输出的 JSON
 * @param {{ ok: boolean, status: string, summary: string }} built 各工具自己写的结论
 * @param {string} fallback 内核没给出可读结果时的那句人话
 * @returns {{ ok: boolean, status: string, summary: string, error: string, detail: object }}
 */
function envelope(json, built, fallback) {
  if (json === null || typeof json !== 'object') {
    return { ok: false, status: '读不懂内核输出', summary: fallback, error: fallback, detail: {} }
  }
  const ok = json.ok === true
  return {
    ok,
    status: built.status,
    summary: built.summary,
    error: ok ? '' : reasonOf(json),
    detail: json,
  }
}

/**
 * 统一返回形状的 schema（10 个工具共用，version 除外——它是先跑通的那一个）。
 *
 * detail 故意不声明字段：内核的输出结构有十几种，逐字段抄一遍既容易抄错，
 * 也会因为内核加个字段就把校验搞崩。它只是"原始回执"，给人深挖用。
 */
const ENVELOPE_SCHEMA = {
  type: 'object',
  properties: {
    ok: { type: 'boolean', description: '这件事成没成' },
    status: { type: 'string', description: '内核给的一句话状态，例如"已启动""部分完成"' },
    summary: { type: 'string', description: '人话结论，可直接念给用户听' },
    error: { type: 'string', description: '没成时的原因；成了就是空串' },
    detail: { type: 'object', description: '内核原始回执（字段随命令不同，供需要时深挖）' },
  },
  required: ['ok', 'status', 'summary', 'error', 'detail'],
  additionalProperties: false,
}

const ENVELOPE_REQUIRED = ['ok', 'status', 'summary', 'error', 'detail']

/** 统一渲染：一句话结论就是给人看的那段。 */
function renderSummary(info) {
  return [{ type: 'text', text: info.summary || info.error || '（内核没有返回可读内容）' }]
}

/** 造一个"统一形状"的工具定义，省掉 9 次重复的样板。 */
function makeTool(spec) {
  return {
    name: spec.name,
    description: spec.description,
    parameters: spec.parameters,
    output: {
      schema: ENVELOPE_SCHEMA,
      render: (args, info) => renderSummary(info),
    },
    execute: spec.execute,
  }
}

/* -------------------------------------------------------------------------- */
/* 共用：跑一次命令 + 兜底                                                       */
/* -------------------------------------------------------------------------- */
/**
 * 跑内核命令并把结果交给 build 生成结论；内核完全读不懂时统一给兜底人话。
 *
 * @param {object} ctx0 { paths, exec }
 * @param {string[]} cmdArgs 子命令与参数（不含脚本路径）
 * @param {number} timeoutMs 超时
 * @param {(json: object) => { ok: boolean, status: string, summary: string }} build
 */
async function invoke(ctx0, cmdArgs, timeoutMs, build) {
  ctx0.exec?.signal?.throwIfAborted()
  const { json, stderr, code, timedOut } = await runJson(
    ctx0.paths.pythonPath,
    [ctx0.paths.scriptPath, ...cmdArgs, '--json'],
    timeoutMs,
    ctx0.exec?.signal,
  )
  const fallback = timedOut
    ? `等了 ${Math.round(timeoutMs / 1000)} 秒还没回话，我先不等了（数据没丢，可以用 hermes_status 再看一次）。`
    : notInstalledHint(ctx0.paths, stderr) || `内核没有返回可读结果（退出码 ${code}）。`
  return envelope(json, json && typeof json === 'object' ? build(json) : { ok: false, status: '', summary: '' }, fallback)
}

/* -------------------------------------------------------------------------- */
/* 各工具的结论生成（把内核 JSON 翻成人话）                                        */
/* -------------------------------------------------------------------------- */

/** status：分两种形态——"看某个任务"和"看整个环境"。 */
function summarizeStatus(j) {
  if (j.ok !== true) {
    return { ok: false, status: statusOf(j), summary: `查状态没查成：${reasonOf(j) || '内核没给原因'}` }
  }
  // --task 模式：内核返回的是任务详情（有 task_dir，没有 lock）
  if (typeof j.task_dir === 'string' && !('lock' in j)) {
    const head = j.running
      ? `任务「${j.name}」正在跑：已拿 ${j.contents} 条内容 / 请求 ${j.requested} 条（${j.percent}%）`
      : `任务「${j.name}」已结束：${j.status || '未知'}，内容 ${j.contents} 条 / 请求 ${j.requested} 条（${j.percent}%）`
    const tail = []
    if (j.comments) tail.push(`评论 ${j.comments} 条`)
    if (j.progress) tail.push(`当前进度：${j.progress}`)
    if (!j.running && reasonOf(j)) tail.push(`原因：${j.reason}`)
    if (j.report) tail.push(`报告：${j.report}`)
    return { ok: true, status: j.running ? '进行中' : (j.status || '已结束'), summary: [head, ...tail].join('\n') }
  }
  // 环境模式
  const lines = []
  const lock = j.lock || {}
  if (lock.held && lock.alive) {
    lines.push(`现在有任务在跑：${lock.describe || lock.task || '（名字没登记）'}`)
    if (j.progress) lines.push(`进度：${j.progress}`)
  } else {
    lines.push('现在没有任务在跑。')
  }
  if (j.cancel_pending) lines.push('已经有人喊停，正在收尾。')
  if (Array.isArray(j.leftover_browsers) && j.leftover_browsers.length > 0) {
    lines.push(`有 ${j.leftover_browsers.length} 个爬虫浏览器窗口没关干净（不影响下次跑，但占内存）。`)
  }
  const hist = Array.isArray(j.history) ? j.history.slice(0, 5) : []
  if (hist.length > 0) {
    lines.push('最近的任务：')
    for (const h of hist) {
      // 内核的 reason 可能是多行（带进程清单），只取第一行；结尾的冒号说明话还没说完，用省略号收住
      let why = reasonOf(h).split('\n')[0].trim()
      if (/[：:]$/.test(why)) why = `${why.slice(0, -1)}…`
      lines.push(`  · ${h.ts}「${h.name}」${h.status}，内容 ${h.contents}/${h.requested} 条${why ? `（${why}）` : ''}`)
    }
  } else {
    lines.push('还没有跑过任何任务。')
  }
  return { ok: true, status: lock.held && lock.alive ? '有任务在跑' : '空闲', summary: lines.join('\n') }
}

/** defaults：列出来，或者报"改了哪几个"。 */
function summarizeDefaults(j) {
  if (j.ok !== true) {
    return { ok: false, status: statusOf(j), summary: `默认参数没读成：${reasonOf(j) || '内核没给原因'}` }
  }
  const d = j.defaults || {}
  const changed = j.changed && typeof j.changed === 'object' ? Object.entries(j.changed) : []
  const lines = []
  if (changed.length > 0) {
    lines.push('已改默认参数：' + changed.map(([k, v]) => `${k}=${v}`).join('、'))
  } else {
    lines.push('当前默认参数：')
  }
  for (const [k, v] of Object.entries(d)) lines.push(`  · ${k} = ${v}`)
  // 内核无改动时也回 changed: {}（空对象是真值），所以必须看条目数而不是对象本身
  return { ok: true, status: changed.length > 0 ? '已保存' : '当前默认参数', summary: lines.join('\n') }
}

/** login：只查票，不弹浏览器（弹窗要人扫码，AI 等不了）。 */
function summarizeLogin(j) {
  const platform = typeof j.platform === 'string' ? j.platform : ''
  const msg = typeof j.message === 'string' ? j.message : ''
  if (j.logged_in === true) {
    return { ok: true, status: '已登录', summary: `${platform} 登录态正常，可以直接开爬。` }
  }
  const need = j.need_user_action === true
  const head = need
    ? `${platform} 需要补登录票：这一步必须有人扫码或导入，AI 自己过不去。`
    : `${platform} 登录态有问题：${msg}`
  return { ok: false, status: statusOf(j) || '未登录', summary: [head, msg ? `内核原话：${msg}` : ''].filter(Boolean).join('\n') }
}

/** estimate：把"要多久、靠不靠得住、有哪些坑"念出来。 */
function summarizeEstimate(j) {
  if (j.ok !== true) {
    return { ok: false, status: statusOf(j), summary: `预估没做成：${reasonOf(j) || '内核没给原因'}` }
  }
  const lines = [
    `预估（${j.platform}｜${j.type}）：请求 ${j.requested} 条内容`
      + (j.comments_on ? `，评论最多 ${j.max_comments_total} 条` : '（不抓评论）'),
    `大概耗时：${j.eta_text}；把握：${j.confidence_label}`,
    `依据：${j.basis}`,
  ]
  if (j.history_expect) lines.push(`历史对照：${j.history_expect}`)
  const cautions = Array.isArray(j.cautions) ? j.cautions : []
  if (cautions.length > 0) {
    lines.push('要注意：')
    for (const c of cautions.slice(0, 6)) lines.push(`  · ${c}`)
  }
  return { ok: true, status: '预估完成', summary: lines.join('\n') }
}

/** run：后台模式回"已启动"，前台模式回"跑完了、拿到多少"。 */
function summarizeRun(j) {
  if (j.ok !== true) {
    return { ok: false, status: statusOf(j) || '没跑成', summary: `没跑成：${reasonOf(j) || '内核没给原因'}` }
  }
  // 后台启动的回执（有 how_to_check 字段）
  if (typeof j.how_to_check === 'string') {
    const lines = [
      `${j.status}｜任务「${j.task}」｜平台 ${j.platform}｜请求 ${j.requested} 条`,
      j.note || '',
      `看进度：再调 hermes_status（task 填「${j.task}」）`,
      `想停：调 hermes_cancel`,
    ].filter(Boolean)
    return { ok: true, status: statusOf(j), summary: lines.join('\n') }
  }
  // 前台跑完的回执
  const lines = [
    `${j.status}｜任务「${j.name}」｜内容 ${j.contents}/${j.requested} 条`
      + (j.comments ? `，评论 ${j.comments} 条` : '')
      + `｜耗时 ${j.duration_s} 秒`,
  ]
  if (reasonOf(j)) lines.push(`原因：${j.reason}（${j.reason_kind_label || ''}）`)
  if (j.advice) lines.push(`建议：${j.advice}`)
  if (j.task_dir) lines.push(`任务目录：${j.task_dir}`)
  if (j.primary_file) lines.push(`数据文件：${j.primary_file}`)
  return { ok: true, status: statusOf(j), summary: lines.join('\n') }
}

/** batch：后台回"队列已起"，前台回"几个成几个败"。 */
function summarizeBatch(j) {
  if (j.ok !== true && !Array.isArray(j.tasks)) {
    return { ok: false, status: statusOf(j) || '没跑成', summary: `队列没起来：${reasonOf(j) || '内核没给原因'}` }
  }
  if (typeof j.how_to_check === 'string') {
    return {
      ok: true,
      status: statusOf(j),
      summary: [`${j.status}｜${j.note || ''}`, `看进度：调 hermes_status（不填 task 看整条队列）`, '想停：调 hermes_cancel'].join('\n'),
    }
  }
  const lines = [`队列跑完：${j.ok_count}/${j.total} 个任务拿到数据。`]
  for (const t of (j.tasks || []).slice(0, 20)) {
    lines.push(`  · 「${t.name}」${t.status}，内容 ${t.contents}/${t.requested} 条${reasonOf(t) ? `（${t.reason}）` : ''}`)
  }
  const kinds = j.failed_by_kind && typeof j.failed_by_kind === 'object' ? Object.entries(j.failed_by_kind) : []
  if (kinds.length > 0) lines.push('失败分类：' + kinds.map(([k, n]) => `${k}×${n}`).join('、'))
  return { ok: j.ok === true, status: statusOf(j), summary: lines.join('\n') }
}

/** resume：补抓补了多少、去重丢了多少。 */
function summarizeResume(j) {
  if (!Array.isArray(j.runs) && j.ok !== true) {
    return { ok: false, status: statusOf(j) || '没跑成', summary: `续跑没起来：${reasonOf(j) || '内核没给原因'}` }
  }
  if (typeof j.how_to_check === 'string') {
    return {
      ok: true,
      status: statusOf(j),
      summary: [`${j.status}｜${j.note || ''}`, `看进度：调 hermes_status（task 填「${j.task}」）`, '想停：调 hermes_cancel'].join('\n'),
    }
  }
  const lines = [
    `${j.status}｜每个关键词目标 ${j.count_per_keyword} 条`,
    `旧数据 ${j.old_contents} 条 + 新抓 ${j.new_contents} 条 → 去重丢掉 ${j.duplicates_dropped} 条 → 合计 ${j.merged_contents} 条内容`,
    `评论合计 ${j.merged_comments} 条`,
  ]
  for (const r of (j.runs || []).slice(0, 20)) {
    lines.push(`  · 「${r.keyword}」${r.status}，新数据 ${r.new_rows} 条${reasonOf(r) ? `（${r.reason}）` : ''}`)
  }
  if (j.out_dir) lines.push(`输出目录：${j.out_dir}`)
  return { ok: j.ok === true, status: statusOf(j), summary: lines.join('\n') }
}

/** cancel：停住没有、数据还在不在。 */
function summarizeCancel(j) {
  const msg = typeof j.note === 'string' ? j.note : reasonOf(j)
  if (j.ok !== true) {
    return { ok: false, status: statusOf(j) || '喊停失败', summary: `喊停没成功：${msg || '内核没给原因'}` }
  }
  const head = `${j.status}${j.task ? `｜任务「${j.task}」` : ''}${j.pid ? `｜进程 ${j.pid}` : ''}`
  return { ok: true, status: statusOf(j), summary: [head, msg].filter(Boolean).join('\n') }
}

/** export：导到哪个文件、每类多少行、有没有校验问题。 */
function summarizeExport(j) {
  if (j.ok !== true) {
    return { ok: false, status: statusOf(j) || '导出失败', summary: `导出失败：${reasonOf(j) || '内核没给原因'}` }
  }
  const lines = [`${j.status}｜文件：${j.file}`]
  for (const s of (j.sheets || [])) lines.push(`  · ${s.kind_cn}：${s.rows} 行 × ${s.cols} 列（sheet「${s.sheet}」）`)
  for (const s of (j.unified_sheets || [])) lines.push(`  · 统一格式页「${s.sheet}」：${s.rows} 行`)
  const skipped = Array.isArray(j.skipped_kinds) ? j.skipped_kinds : []
  if (skipped.length > 0) lines.push(`没数据、跳过没导的：${skipped.join('、')}`)
  const problems = Array.isArray(j.problems) ? j.problems : []
  if (problems.length > 0) {
    lines.push('回读校验有疑点：')
    for (const p of problems.slice(0, 5)) lines.push(`  · ${p}`)
  } else {
    lines.push('回读校验通过（表头和行数都对得上）。')
  }
  return { ok: true, status: statusOf(j), summary: lines.join('\n') }
}

/* -------------------------------------------------------------------------- */
/* 工具定义                                                                     */
/* -------------------------------------------------------------------------- */

/** 通用：平台说明（和内核的支持列表保持一致）。 */
const PLATFORM_DESCRIPTION =
  '平台：bili（B站）/ dy（抖音）/ ks（快手）/ wb（微博）/ tieba（贴吧）/ zhihu（知乎）。'
  + '支持中文别名（B站、抖音、微博…）。xhs（小红书）反爬最狠，能用但不保证。'

const COMMON_RUN_PROPS = {
  platform: { type: 'string', description: PLATFORM_DESCRIPTION },
  keywords: { type: 'string', description: '关键词，多个用英文逗号分隔，上限 10 个' },
  count: { type: 'integer', description: '每个关键词抓多少条内容（不给就用默认值）' },
  format: { type: 'string', enum: ['json', 'jsonl', 'csv', 'excel'], description: '落盘格式（不给就用默认值）' },
  type: { type: 'string', enum: ['search', 'detail', 'creator'], description: 'search=按关键词搜；detail=按内容ID抓详情；creator=按创作者主页抓' },
  comments: { type: 'boolean', description: '要不要抓评论；不给就用默认值' },
  max_comments: { type: 'integer', description: '每条内容最多抓多少条评论（不给就用默认值）' },
  timeout_min: { type: 'integer', description: '这次最多跑多少分钟，到点自动收工、已落盘数据保留（不给就按数量自动算）' },
  specified_id: { type: 'string', description: 'type=detail 时要抓的内容 ID，逗号分隔' },
  creator_id: { type: 'string', description: 'type=creator 时要抓的创作者 ID，逗号分隔' },
}

/** 把这些通用参数拼成命令行。 */
function pushRunArgs(args, p) {
  pushValue(args, '--platform', p.platform)
  pushValue(args, '--keywords', p.keywords)
  pushNumber(args, '--count', p.count)
  pushValue(args, '--format', p.format)
  pushValue(args, '--type', p.type)
  pushValue(args, '--specified-id', p.specified_id)
  pushValue(args, '--creator-id', p.creator_id)
  pushToggle(args, '--comments', '--no-comments', p.comments)
  pushNumber(args, '--max-comments', p.max_comments)
  pushNumber(args, '--timeout-min', p.timeout_min)
}

/** 造出本插件要注册的全部工具。 */
function createTools(opts) {
  const paths = resolvePaths(opts)

  return [
    /* ------------------------------ 1. 查版本 ------------------------------ */
    {
      name: 'hermes_version',
      description:
        '查询 Hermes 爬虫插件的版本与环境状态（中文：查版本；English: inspect Hermes crawler plugin version and environment）。'
        + '返回插件版本、这版改了什么、装在哪个目录、用的是哪个 Python、MCP 工具层是否就位。'
        + '当用户问"装好了吗""是什么版本""能用吗"，或爬取报错需要先排查环境时，先调这个。',
      parameters: { type: 'object', properties: {}, additionalProperties: false },
      output: {
        schema: {
          type: 'object',
          properties: {
            ok: { type: 'boolean' },
            pluginVersion: { type: 'string', description: '爬虫插件版本号，例如 1.11.1' },
            note: { type: 'string', description: '这一版改了什么的说明' },
            crawlerRoot: { type: 'string', description: 'MediaCrawler 所在目录' },
            pythonPath: { type: 'string', description: '实际使用的 Python 解释器' },
            mcpServerExists: { type: 'boolean', description: 'MCP 工具层文件是否存在' },
            error: { type: 'string', description: '出问题时的说明；正常时为空串' },
          },
          required: ['ok', 'pluginVersion', 'note', 'crawlerRoot', 'pythonPath', 'mcpServerExists', 'error'],
          additionalProperties: false,
        },
        render: (args, info) => [{ type: 'text', text: renderVersion(info) }],
      },
      execute: async (args, exec) => {
        exec?.signal?.throwIfAborted()

        const { json, stderr, code } = await runJson(
          paths.pythonPath,
          [paths.scriptPath, 'version', '--json'],
          TIMEOUT.version,
          exec?.signal,
        )

        if (json === null || typeof json !== 'object') {
          return {
            ok: false,
            pluginVersion: '',
            note: '',
            crawlerRoot: paths.crawlerRoot,
            pythonPath: paths.pythonPath,
            mcpServerExists: false,
            error: notInstalledHint(paths, stderr) || `内核没有返回可读结果（退出码 ${code}）。`,
          }
        }

        return {
          ok: json.ok === true,
          pluginVersion: typeof json.version === 'string' ? json.version : '',
          note: typeof json.note === 'string' ? json.note : '',
          crawlerRoot: paths.crawlerRoot,
          pythonPath: typeof json.python === 'string' ? json.python : paths.pythonPath,
          mcpServerExists: json.mcp_server_exists === true,
          error: json.ok === true ? '' : reasonOf(json),
        }
      },
    },

    /* ------------------------------ 2. 查状态 ------------------------------ */
    makeTool({
      name: 'hermes_status',
      description:
        '看 Hermes 的当前状态：有没有任务在跑、进度多少、最近跑过哪些任务（中文：查状态；English: check Hermes runtime status and recent tasks）。'
        + '给 task 就只看那一个任务的详情（跑没跑完、拿到几条、报告在哪）；不给就返回整机环境快照。'
        + '这是只读操作，随时可以调。用户问"跑完了吗""到哪一步了"，或者你刚启动后台任务想知道结果，都调这个。',
      parameters: {
        type: 'object',
        properties: {
          task: { type: 'string', description: '任务名（可以只给片段）或任务目录路径；不填就看整体环境' },
        },
        additionalProperties: false,
      },
      execute: async (args, exec) => {
        const cmd = ['status']
        pushValue(cmd, '--task', args.task)
        return invoke({ paths, exec }, cmd, TIMEOUT.status, summarizeStatus)
      },
    }),

    /* ------------------------------ 3. 默认值 ------------------------------ */
    makeTool({
      name: 'hermes_defaults',
      description:
        '查看或修改 Hermes 的默认参数（中文：默认参数；English: read or update Hermes default settings）。'
        + '不给 set 就只是读一遍；给了就改成新的并记住，下次 run 不写参数时会用这些值。'
        + '可改的项：count、format、type、comments、max_comments、media、headless、retries、count_cap、timeout_base_min、timeout_max_min。'
        + '用户说"以后默认抓 30 条""默认要 Excel"这类话时用。',
      parameters: {
        type: 'object',
        properties: {
          set: {
            type: 'array',
            items: { type: 'string' },
            description: '要改的项，写成 "键=值"，例如 ["count=30","format=excel"]；不填就是只读',
          },
        },
        additionalProperties: false,
      },
      execute: async (args, exec) => {
        const cmd = ['defaults']
        if (Array.isArray(args.set) && args.set.length > 0) cmd.push('--set', ...args.set)
        return invoke({ paths, exec }, cmd, TIMEOUT.defaults, summarizeDefaults)
      },
    }),

    /* ------------------------------ 4. 查登录 ------------------------------ */
    makeTool({
      name: 'hermes_login',
      description:
        '检查某个平台的登录票还有效吗（中文：查登录态；English: check whether Hermes still has a valid login session for a platform）。'
        + '只查不弹窗：不会启动浏览器、不会要求扫码，AI 可以随时调。'
        + '返回 need_user_action=true 时说明必须由人来补票（扫码或从浏览器导入），把这句话原样转告用户，不要自己硬试。',
      parameters: {
        type: 'object',
        properties: {
          platform: { type: 'string', description: PLATFORM_DESCRIPTION },
        },
        required: ['platform'],
        additionalProperties: false,
      },
      execute: async (args, exec) => {
        const cmd = ['login', '--check-only']
        pushValue(cmd, '--platform', args.platform)
        return invoke({ paths, exec }, cmd, TIMEOUT.loginCheck, summarizeLogin)
      },
    }),

    /* ------------------------------ 5. 预估 ------------------------------- */
    makeTool({
      name: 'hermes_estimate',
      description:
        '只估算不真跑：这次大概能拿多少条、要等多久、会不会白跑（中文：预估；English: estimate duration and yield without crawling）。'
        + '不启动浏览器、不发网络请求、不动任何数据。依据是本机历史实测 + 超时公式，没有历史就明说估不准。'
        + '用户问"要多久""值不值得跑""这个量会不会超时"，或你要在开跑前给个交底时，先调这个。',
      parameters: {
        type: 'object',
        properties: COMMON_RUN_PROPS,
        required: ['platform', 'keywords'],
        additionalProperties: false,
      },
      execute: async (args, exec) => {
        const cmd = ['estimate']
        pushRunArgs(cmd, args)
        return invoke({ paths, exec }, cmd, TIMEOUT.estimate, summarizeEstimate)
      },
    }),

    /* ------------------------------ 6. 开跑 ------------------------------- */
    makeTool({
      name: 'hermes_run',
      description:
        '真去爬一个任务（中文：开始爬取；English: start one crawler task）。'
        + '默认后台跑：十几秒内返回"已启动"，然后你用 hermes_status 看进度——推荐，因为爬取常常几十分钟，前台干等会超时。'
        + '把 detach 设成 false 才是前台等它跑完（最多等 30 分钟，且会占着这次对话）。'
        + '跑完数据落在任务目录里；要 Excel 交付再调 hermes_export。',
      parameters: {
        type: 'object',
        properties: {
          ...COMMON_RUN_PROPS,
          name: { type: 'string', description: '任务名（默认取第一个关键词）；目录名会自动加平台和日期' },
          media: { type: 'boolean', description: '是否下载图片/视频文件（很占磁盘和时间，默认不下载）' },
          headless: { type: 'boolean', description: '是否隐藏浏览器窗口；默认开着窗口（反爬更稳）' },
          retries: { type: 'integer', description: '遇到网络类错误自动重试几次，0~3' },
          wait_lock: { type: 'number', description: '别人正在跑时最多等几分钟；默认 0 = 不抢，直接告诉你忙' },
          probe: { type: 'boolean', description: '开跑前先探一次票（多花 20~40 秒换"别白跑几十分钟"）；不给就自动判断' },
          detach: { type: 'boolean', description: 'true（默认）=后台跑立刻返回；false=前台等它跑完' },
        },
        required: ['platform', 'keywords'],
        additionalProperties: false,
      },
      execute: async (args, exec) => {
        const detach = args.detach !== false
        const cmd = ['run']
        pushRunArgs(cmd, args)
        pushValue(cmd, '--name', args.name)
        pushToggle(cmd, '--media', '--no-media', args.media)
        pushToggle(cmd, '--headless', '--no-headless', args.headless)
        pushToggle(cmd, '--probe', '--no-probe', args.probe)
        pushNumber(cmd, '--retries', args.retries)
        pushDecimal(cmd, '--wait-lock', args.wait_lock)
        if (detach) cmd.push('--detach')
        return invoke({ paths, exec }, cmd, detach ? TIMEOUT.detach : TIMEOUT.foreground, summarizeRun)
      },
    }),

    /* ------------------------------ 7. 批量 ------------------------------- */
    makeTool({
      name: 'hermes_batch',
      description:
        '一次性提交多个爬取任务，排队跑（中文：批量；English: queue several crawler tasks）。'
        + '给 tasks 数组，每项和 hermes_run 的参数同名（platform、keywords、count…）。'
        + '默认后台跑整条队列，返回后可用 hermes_status 看进度。适合"这几个关键词/几个平台一起跑"。',
      parameters: {
        type: 'object',
        properties: {
          tasks: {
            type: 'array',
            items: { type: 'object', description: '一个任务，字段和 hermes_run 的参数同名' },
            description: '任务列表；每项至少要有 platform 和 keywords',
          },
          filePath: { type: 'string', description: '已有的 JSON 数组文件路径（和 tasks 二选一，tasks 优先）' },
          detach: { type: 'boolean', description: 'true（默认）=整条队列甩到后台；false=前台等整条跑完' },
        },
        additionalProperties: false,
      },
      execute: async (args, exec) => {
        exec?.signal?.throwIfAborted()
        let filePath = typeof args.filePath === 'string' ? args.filePath.trim() : ''
        const tasks = Array.isArray(args.tasks) ? args.tasks : []
        if (tasks.length > 0) {
          // 内核只吃"一个 JSON 文件"，所以这里替用户落一次盘。
          // 放在 agent_crawler 下的 _agent_batches 里，和内核自己的 _probe / exports 一个路数。
          const dir = join(paths.crawlerRoot, 'agent_crawler', '_agent_batches')
          mkdirSync(dir, { recursive: true })
          const stamp = new Date().toISOString().replace(/[:.]/g, '-')
          filePath = join(dir, `batch_${stamp}.json`)
          writeFileSync(filePath, JSON.stringify(tasks, null, 2), 'utf8')
        }
        if (filePath === '') {
          return {
            ok: false,
            status: '缺参数',
            summary: '没给任务清单：要么给 tasks 数组，要么给 filePath 指向一个 JSON 数组文件。',
            error: 'missing tasks',
            detail: {},
          }
        }
        const detach = args.detach !== false
        const cmd = ['batch', '--file', filePath]
        if (detach) cmd.push('--detach')
        return invoke({ paths, exec }, cmd, detach ? TIMEOUT.detach : TIMEOUT.foreground, summarizeBatch)
      },
    }),

    /* ------------------------------ 8. 续跑 ------------------------------- */
    makeTool({
      name: 'hermes_resume',
      description:
        '接着一个没跑完的旧任务继续抓：按关键词补抓、再按内容 ID 去重合并（中文：断点续跑；English: resume an unfinished task with dedup merge）。'
        + '比重新开一个任务划算——已经抓到的不重复抓，只在后面接着补。'
        + '默认后台跑，返回后可用 hermes_status 看进度。',
      parameters: {
        type: 'object',
        properties: {
          task: { type: 'string', description: '要续跑的旧任务：任务名（可只给片段）或任务目录路径' },
          count: { type: 'integer', description: '每个关键词的目标条数（不给就沿用旧任务的设置）' },
          attempt: { type: 'integer', description: '读第几次尝试的数据（不给就取编号最大且真有数据的那次）' },
          from_scratch: { type: 'boolean', description: 'true=不管抓到哪一步，每个关键词都从第 1 页重抓（仍会去重）' },
          timeout_min: { type: 'integer', description: '每个关键词最多跑多少分钟' },
          detach: { type: 'boolean', description: 'true（默认）=后台跑立刻返回；false=前台等它跑完' },
        },
        required: ['task'],
        additionalProperties: false,
      },
      execute: async (args, exec) => {
        const detach = args.detach !== false
        const cmd = ['resume']
        pushValue(cmd, '--task', args.task)
        pushNumber(cmd, '--count', args.count)
        pushNumber(cmd, '--attempt', args.attempt)
        pushNumber(cmd, '--timeout-min', args.timeout_min)
        if (args.from_scratch === true) cmd.push('--from-scratch')
        if (detach) cmd.push('--detach')
        return invoke({ paths, exec }, cmd, detach ? TIMEOUT.detach : TIMEOUT.foreground, summarizeResume)
      },
    }),

    /* ------------------------------ 9. 喊停 ------------------------------- */
    makeTool({
      name: 'hermes_cancel',
      description:
        '喊停正在跑的任务：不硬杀，递一张纸条让它优雅收尾（关浏览器、写报告），已抓到的数据都保留（中文：停止任务；English: gracefully cancel the running task）。'
        + '用户说"停""别跑了""太久了"时用。喊停后通常还要几秒到几十秒才真正停下，可以用 hermes_status 再确认一次。',
      parameters: {
        type: 'object',
        properties: {
          wait: { type: 'integer', description: '喊停后最多等几秒看它有没有真停（默认 30）；填 0 就是递完纸条立刻返回' },
        },
        additionalProperties: false,
      },
      execute: async (args, exec) => {
        const cmd = ['cancel']
        pushNumber(cmd, '--wait', args.wait)
        return invoke({ paths, exec }, cmd, TIMEOUT.cancel, summarizeCancel)
      },
    }),

    /* ------------------------------ 10. 导出 ------------------------------ */
    makeTool({
      name: 'hermes_export',
      description:
        '把一个已采任务导出成交付用的 Excel（中文：导出表格；English: export a finished task into a deliverable xlsx）。'
        + '只搬不改：从任务目录读原始数据，生成 xlsx，不动原文件。会回读校验表头和行数。'
        + '用户说"导成表格""给我 Excel""发给同事"，或任务跑完后要交付时用。',
      parameters: {
        type: 'object',
        properties: {
          task: { type: 'string', description: '任务名（可只给片段）或任务目录路径' },
          out: { type: 'string', description: '输出 xlsx 路径；不给就放到 agent_crawler\\exports\\ 下自动命名' },
          kinds: { type: 'string', description: '导哪几类，逗号分隔：contents,comments,creators（默认 contents,comments）' },
          attempt: { type: 'integer', description: '指定第几次尝试的数据（不给就取编号最大且真有数据的那次）' },
          unified: { type: 'boolean', description: '是否额外加一个"统一格式"页（默认加；设 false 只要原样表）' },
        },
        required: ['task'],
        additionalProperties: false,
      },
      execute: async (args, exec) => {
        const cmd = ['export']
        pushValue(cmd, '--task', args.task)
        pushValue(cmd, '--out', args.out)
        pushValue(cmd, '--kinds', args.kinds)
        pushNumber(cmd, '--attempt', args.attempt)
        if (args.unified === false) cmd.push('--no-unified')
        return invoke({ paths, exec }, cmd, TIMEOUT.export, summarizeExport)
      },
    }),
  ]
}

/** 把 version 的结果排成模型好读的几行（每行都来自上面校验过的字段，不会自相矛盾）。 */
function renderVersion(info) {
  if (!info.ok) {
    return `Hermes 环境有问题：${info.error}`
  }
  return [
    `Hermes 插件版本：${info.pluginVersion}`,
    `这版内容：${info.note}`,
    `装在：${info.crawlerRoot}`,
    `Python：${info.pythonPath}`,
    `MCP 工具层：${info.mcpServerExists ? '有' : '没有'}`,
  ].join('\n')
}

/**
 * 插件装配入口。
 *
 * 不加 Config schema 是有意的：先用原始 opts 跑通，等要上 dsh 设置页时再补 schemastery。
 *
 * @param {object} ctx cordis 上下文
 * @param {{ crawlerRoot?: string, pythonPath?: string }} [opts] 来自 cordis.patch.yml 的 config
 */
export function apply(ctx, opts) {
  const tools = ctx?.tools
  if (typeof tools?.register !== 'function') {
    // 没有工具注册表就什么都不做——不抛异常，免得整个会话起不来
    console.warn(`${name}: ctx.tools.register 不可用，插件未注册任何工具`)
    return
  }

  for (const tool of createTools(opts)) {
    tools.register(tool)
  }
}
