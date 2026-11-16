import { MAX_CRAWL_COUNT } from '@/types/crawler'
import type { CrawlerConfig } from '@/types/crawler'

/**
 * 把网页版的表单配置翻译成后端 /api/crawler/start 需要的请求体。
 *
 * 后端每种设置只有两种入口：
 *  1. 专用字段（max_notes_count / max_comments_count / save_option ...）
 *  2. config_overrides —— 一个 JSON，交给 cmd_arg/arg.py 的白名单函数写回 config 模块
 * 这里负责把「哪些设置走哪个入口」讲清楚，组件只管收集输入。
 */

/** 数据库保存格式 -> config_overrides.db 里的分组名 */
const DB_SECTION: Record<string, 'mysql' | 'mongodb' | 'postgres'> = {
  db: 'mysql',
  mongodb: 'mongodb',
  postgres: 'postgres',
}

/** 各数据库的默认连接参数（与后端 config/db_config.py 的默认值一致） */
export const DB_DEFAULTS: Record<string, { port: number; user: string; name: string }> = {
  db: { port: 3306, user: 'root', name: 'media_crawler' },
  mongodb: { port: 27017, user: '', name: 'media_crawler' },
  postgres: { port: 5432, user: 'postgres', name: 'media_crawler' },
}

export function isDbSaveOption(saveOption: string): boolean {
  return saveOption in DB_SECTION
}

/** 「词:分组, 词2」-> {"词": "分组", "词2": "自定义"} */
export function parseCustomWords(raw: string): Record<string, string> {
  const words: Record<string, string> = {}
  raw
    .split(/[,，\n]/)
    .map((item) => item.trim())
    .filter(Boolean)
    .forEach((item) => {
      const [word, group] = item.split(/[:：]/)
      const key = (word ?? '').trim()
      if (!key) return
      words[key] = (group ?? '').trim() || '自定义'
    })
  return words
}

/** 抓取数量：<=0 / 非法 -> null（用程序默认值），超过上限 -> 上限 */
function normalizeCount(value: number): number | null {
  if (!Number.isFinite(value)) return null
  const n = Math.floor(value)
  if (n < 1) return null
  return Math.min(n, MAX_CRAWL_COUNT)
}

function normalizeConcurrency(value: number): number | null {
  if (!Number.isFinite(value)) return null
  const n = Math.floor(value)
  if (n < 1) return null
  return Math.min(n, 32)
}

function buildDbOverrides(config: CrawlerConfig): Record<string, unknown> | undefined {
  const section = DB_SECTION[config.save_option]
  if (!section) return undefined

  const host = config.db_host.trim()
  const dbName = config.db_name.trim()
  if (!host || !dbName) return undefined

  const credentials: Record<string, unknown> = { host, db_name: dbName }
  const port = normalizeCount(config.db_port)
  if (port !== null) credentials.port = port
  const user = config.db_user.trim()
  if (user) credentials.user = user
  if (config.db_password !== '') credentials.password = config.db_password

  return { [section]: credentials }
}

/** 第三方代理服务商需要的凭据 -> 环境变量名（后端会再过滤一次） */
function buildProxyCredentials(config: CrawlerConfig): Record<string, string> {
  const credentials: Record<string, string> = {}
  const put = (name: string, value: string) => {
    const trimmed = value.trim()
    if (trimmed) credentials[name] = trimmed
  }
  if (config.ip_proxy_provider_name === 'kuaidaili') {
    put('KDL_SECERT_ID', config.kdl_secret_id)
    put('KDL_SIGNATURE', config.kdl_signature)
    put('KDL_USER_NAME', config.kdl_user_name)
    put('KDL_USER_PWD', config.kdl_user_pwd)
  } else if (config.ip_proxy_provider_name === 'wandouhttp') {
    put('WANDOU_APP_KEY', config.wandou_app_key)
  }
  return credentials
}

/** 收集所有「没有专用字段」的设置，交给后端的白名单函数写回 config */
export function buildConfigOverrides(config: CrawlerConfig): Record<string, unknown> {
  const cfg: Record<string, unknown> = {}

  // 速度与稳定性
  // 间隔允许填 0（不等待），空输入也按 0 处理；其它数值原样下发
  const sleepSec = Number.isFinite(config.sleep_sec) ? Math.max(0, Math.floor(config.sleep_sec)) : 0
  cfg.CRAWLER_MAX_SLEEP_SEC = sleepSec
  cfg.DOWNLOAD_LIVE_PHOTO = config.download_live_photo

  // 平台专属
  if (config.platform === 'xhs') {
    cfg.SORT_TYPE = config.xhs_sort
  } else if (config.platform === 'dy') {
    cfg.PUBLISH_TIME_TYPE = config.dy_publish_time
  } else if (config.platform === 'bili') {
    cfg.BILI_SEARCH_MODE = config.bili_search_mode
    // 清空 / 非法值就沿用程序默认（清晰度 80，每日上限 1）
    const qn = normalizeCount(config.bili_qn)
    if (qn !== null) cfg.BILI_QN = qn
    const notesPerDay = normalizeCount(config.bili_max_notes_per_day)
    if (notesPerDay !== null) cfg.MAX_NOTES_PER_DAY = notesPerDay
    // 只有「时间区间」两种模式才会用到日期
    if (config.bili_search_mode !== 'normal') {
      if (config.bili_start_day) cfg.START_DAY = config.bili_start_day
      if (config.bili_end_day) cfg.END_DAY = config.bili_end_day
    }
  } else if (config.platform === 'wb') {
    cfg.ENABLE_WEIBO_FULL_TEXT = config.weibo_full_text
  }

  // 词云调优
  cfg.CUSTOM_WORDS = parseCustomWords(config.custom_words)
  if (config.stop_words_file.trim()) cfg.STOP_WORDS_FILE = config.stop_words_file.trim()
  if (config.font_path.trim()) cfg.FONT_PATH = config.font_path.trim()

  const db = buildDbOverrides(config)
  return db ? { config: cfg, db } : { config: cfg }
}

/** 表单配置 -> POST /api/crawler/start 的请求体 */
export function toStartPayload(config: CrawlerConfig) {
  return {
    platform: config.platform,
    login_type: config.login_type,
    crawler_type: config.crawler_type,
    keywords: config.keywords,
    specified_ids: config.specified_ids,
    creator_ids: config.creator_ids,
    start_page: config.start_page,
    enable_comments: config.enable_comments,
    enable_sub_comments: config.enable_sub_comments,
    enable_media: config.enable_media,
    enable_wordcloud: config.enable_wordcloud,
    enable_excel_export: config.enable_excel_export,
    save_option: config.save_option,
    cookies: config.cookies,
    headless: config.headless,

    max_notes_count: normalizeCount(config.max_notes_count),
    max_comments_count: normalizeCount(config.max_comments_count),
    max_concurrency: normalizeConcurrency(config.max_concurrency),
    save_data_path: config.save_data_path.trim(),

    enable_ip_proxy: config.enable_ip_proxy,
    ip_proxy_pool_count: config.enable_ip_proxy ? normalizeCount(config.ip_proxy_pool_count) : null,
    ip_proxy_provider_name: config.enable_ip_proxy ? config.ip_proxy_provider_name : '',
    static_proxy_url: config.enable_ip_proxy ? config.static_proxy_url.trim() : '',
    ip_proxy_credentials: config.enable_ip_proxy ? buildProxyCredentials(config) : {},

    config_overrides: buildConfigOverrides(config),
  }
}

export type StartCrawlerPayload = ReturnType<typeof toStartPayload>
