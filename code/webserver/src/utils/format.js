/* ============================================================
 * 展示层格式化 —— 一处转换，页面里不散落着正则与 toFixed
 * ============================================================
 * 两条容易踩的坑都在这里处理掉了：
 *   ① **时区**：`pb_photo.takenAt` 是 **UTC ISO8601**（"2023-05-01T04:00:00Z"，
 *      见 scanner/meta.py）。直接 `slice(0,10)` 截出来的是 UTC 日期，
 *      东八区凌晨 1 点拍的照片会显示成前一天。所以一律走 Date 转本地再格式化。
 *   ② **空值**：EXIF 读不出来的照片 takenAt / shotYear 都是 null，
 *      直接渲染会得到 "null" 字样或者 Invalid Date。
 */

/** 空值占位符：统一成一个，页面里不各写各的 */
export const EMPTY = '—'

/** 把 ISO8601 / "YYYY-MM-DD HH:MM:SS" / "YYYYMMDDHHMMSS" 解析成 Date；失败返回 null */
export function parseTime(value) {
  if (!value) return null
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value
  let text = String(value).trim()
  if (!text) return null
  if (/^\d{14}$/.test(text)) {
    // YYYYMMDDHHMMSS（库里 modifyYMDHMS 那种）
    text = `${text.slice(0, 4)}-${text.slice(4, 6)}-${text.slice(6, 8)}T${text.slice(8, 10)}:${text.slice(10, 12)}:${text.slice(12, 14)}`
  } else if (/^\d{8}$/.test(text)) {
    text = `${text.slice(0, 4)}-${text.slice(4, 6)}-${text.slice(6, 8)}T00:00:00`
  } else if (/^\d{4}-\d{2}-\d{2} /.test(text)) {
    text = text.replace(' ', 'T')
  }
  const date = new Date(text)
  return Number.isNaN(date.getTime()) ? null : date
}

function pad(n) {
  return String(n).padStart(2, '0')
}

/** 本地日期 `YYYY-MM-DD` */
export function formatDate(value) {
  const date = parseTime(value)
  if (!date) return EMPTY
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

/** 本地日期时间 `YYYY-MM-DD HH:MM` */
export function formatDateTime(value) {
  const date = parseTime(value)
  if (!date) return EMPTY
  return `${formatDate(date)} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

/** 拍摄年份（优先 shotYear，其次 takenAt） */
export function formatYear(photo) {
  if (!photo) return EMPTY
  if (photo.shotYear !== null && photo.shotYear !== undefined && photo.shotYear !== '') {
    return String(photo.shotYear)
  }
  const date = parseTime(photo.takenAt)
  return date ? String(date.getFullYear()) : EMPTY
}

/** 年代桶 `2010-2014` -> `2010–2014`（界面用连接号更好读） */
export function formatBucketKey(bucketKey) {
  const text = String(bucketKey || '').trim()
  if (!text) return EMPTY
  if (text === 'ALL') return '全部年代'
  return text.replace('-', '–')
}

/** 文件字节数 -> 人类可读 */
export function formatFileSize(bytes) {
  const num = Number(bytes)
  if (!Number.isFinite(num) || num <= 0) return EMPTY
  const units = ['B', 'KB', 'MB', 'GB']
  let value = num
  let index = 0
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024
    index += 1
  }
  return `${value >= 100 || index === 0 ? Math.round(value) : value.toFixed(1)} ${units[index]}`
}

/** 千分位（中文语境用逗号） */
export function formatCount(value) {
  const num = Number(value)
  if (!Number.isFinite(num)) return '0'
  return num.toLocaleString('zh-CN')
}

/** 路径 -> 文件名（列表里路径很长，只显示末段） */
export function baseName(relPath) {
  const text = String(relPath || '')
  const index = Math.max(text.lastIndexOf('/'), text.lastIndexOf('\\'))
  return index >= 0 ? text.slice(index + 1) : text
}

/** hash 缩略显示：`sha256:9f3a…2b71`（侧栏只给前后 8 位，够核对即可） */
export function shortHash(hash, head = 8, tail = 6) {
  const text = String(hash || '').trim()
  if (!text) return EMPTY
  if (text.length <= head + tail + 1) return text
  return `${text.slice(0, head)}…${text.slice(-tail)}`
}

/**
 * 缩略图显示尺寸（4:3 还是 1:1）。
 * EXIF orientation ∈ {5,6,7,8} 时宽高是转置的，按**显示**方向取。
 */
export function displaySize(photo) {
  if (!photo) return { width: 0, height: 0 }
  let width = Number(photo.width) || 0
  let height = Number(photo.height) || 0
  const orientation = Number(photo.orientation) || 1
  if (orientation >= 5 && orientation <= 8) {
    const tmp = width
    width = height
    height = tmp
  }
  return { width, height }
}
