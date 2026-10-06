/* ============================================================
 * 静态资源 URL 构造（api/static.py）
 * ============================================================
 * ⚠️ 这一层**不用 axios**：图片是给 <img src> 用的，走 axios 反而要手动
 *   blob 化再 revokeObjectURL，徒增内存与生命周期管理成本。
 *
 * 缩略图规则（设计稿红线）：
 *   · 网格里**只允许**出现 /thumb，绝不加载原图；
 *   · 缩略图路径可由 fileHash 推导、带 ETag 与 Cache-Control，二次访问命中磁盘缓存；
 *   · 三个尺寸 200 / 400 / 800，按展示密度选。
 *
 * 原图只读、支持 Range（<video>/<img> 与浏览器的分段请求都依赖它）。
 * 人脸裁剪图默认 160×160 正方形（步骤 4），可直接当头像用，前端不必再裁。
 */
const BASE = import.meta.env.VITE_API_BASE || ''

function withSize(photoCode, size) {
  return `${BASE}/api/thumb/${encodeURIComponent(photoCode)}?size=${size}`
}

/** 缩略图 URL。size ∈ 200 | 400 | 800，默认 400（网格用 200、详情用 800） */
export function thumbUrl(photoCode, size = 400) {
  return withSize(photoCode, size)
}

/** 原图 URL（只在详情页用；支持 Range，勿加时间戳破坏缓存） */
export function originalUrl(photoCode) {
  return `${BASE}/api/original/${encodeURIComponent(photoCode)}`
}

/** 人脸裁剪图 URL（160px 正方形，可直接圆形遮罩渲染） */
export function faceUrl(faceCode) {
  return `${BASE}/api/face/${encodeURIComponent(faceCode)}`
}

/** 缩略图目录统计（排障用，不用于业务判断） */
export function mediaStats() {
  return `${BASE}/api/media/stats`
}