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
 *
 * ⚠️ BASE 的口径（2026-10-08 修）：**必须是含 `/api` 的那个 base**，
 *   所以这里直接复用 request.js 的 API_BASE，而不是自己再读一遍 env。
 *   原来的写法是 `import.meta.env.VITE_API_BASE || ''` 然后自己拼 `/api/...`，
 *   与 request.js（`VITE_API_BASE || '/api'`，调用路径不再带 `/api`）**要求相反**：
 *     · .env.development 按旧口径写（不含 /api）→ 缩略图/原图对，
 *       但 axios 的数据请求打到 `/photos`，被后端 StaticFiles 的 SPA 回退
 *       返回 index.html，**200 + HTML**，`data?.items ?? []` 吃掉它 ⇒
 *       照片流「共 0 张照片」而控制台一条错都没有 —— 项目自己文件头警告过的那一类；
 *     · 改成含 /api → 数据对了，图片 URL 变成 `/api/api/...` ⇒ 满屏 404。
 *   两个方向必错一个，所以只能有一个定义。现在从 request.js 引，drift 无从发生。
 */
import { API_BASE } from './request'

function withSize(photoCode, size) {
  return `${API_BASE}/thumb/${encodeURIComponent(photoCode)}?size=${size}`
}

/** 缩略图 URL。size ∈ 200 | 400 | 800，默认 400（网格用 200、详情用 800） */
export function thumbUrl(photoCode, size = 400) {
  return withSize(photoCode, size)
}

/** 原图 URL（只在详情页用；支持 Range，勿加时间戳破坏缓存） */
export function originalUrl(photoCode) {
  return `${API_BASE}/original/${encodeURIComponent(photoCode)}`
}

/** 人脸裁剪图 URL（160px 正方形，可直接圆形遮罩渲染） */
export function faceUrl(faceCode) {
  return `${API_BASE}/face/${encodeURIComponent(faceCode)}`
}

/** 缩略图目录统计（排障用，不用于业务判断） */
export function mediaStats() {
  return `${API_BASE}/media/stats`
}