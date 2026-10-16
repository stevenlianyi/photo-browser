/* ============================================================
 * 照片详情页的「返回」与「翻页范围」约定
 * ============================================================
 * 背景：`/photos/:photoCode` 是**多入口**页面 —— 照片流、人物详情、地点详情、
 * 待确认、概览、重复对比都能点进来。返回键若写死 `to="/photos"`，
 * 从人物库进来的人会被扔回照片流，与浏览器后退键的行为不一致。
 *
 * 一、返回：入口声明来源，详情页负责回退
 * ------------------------------------------------------------------
 *   ① 入口跳转时带 `?from=<来源页完整路径>&fromName=<来源显示名>`；
 *   ② 主返回按钮 = 浏览器回退语义（`router.back()`）——
 *      来源页的滚动位置、筛选条件由浏览器还原，和后退键完全一致；
 *   ③ 只有历史栈里没有站内上一页（刷新 / 新标签打开 / 分享链接）时，
 *      才用 `router.replace(from)` 兜底。
 *      ⚠️ 必须 replace：用 push 会在历史里再叠一条详情页，用户再按返回又回到
 *      详情页 —— 「返回」变成一个跳不出去的循环。
 *   ④ `fromName` 只用于按钮文案（「← 返回 白瑞琴」比「← 返回上一页」有信息量），
 *      缺失时退回「← 返回上一页」。列表页另有**固定次级入口**（「照片流」/
 *      「人物库」），不参与回退 —— 「我从哪来」和「我要看全库」两类需求都要满足。
 *   ⑤ `goBackOr()` 是**通用**的：人物详情 / 地点详情页头的返回同样用它。
 *      写死 RouterLink 的毛病（页面按钮与浏览器后退键行为不一致）在这几处
 *      一模一样，不要在那儿另写一套。
 *
 * 二、翻页范围：`?scope=<type>:<code>[@<extra>]`
 * ------------------------------------------------------------------
 *   详情页的左右箭头默认翻**全库照片流**（`store.photos.items`）。
 *   从人物 / 地点详情进来时这是错的：在「白瑞琴的照片」里翻两张就翻到陌生人的
 *   照片上，而界面上没有任何东西提示这件事发生了。所以入口声明 `scope`，
 *   详情页改沿**那一批照片**翻：
 *     · `person:<personCode>`         —— 该人物的照片（/api/photos?personCode=）
 *     · `place:<placeCode>[@<codes>]` —— 该地点的照片（/places/{code}/photos）；
 *       `@` 后面是「同组归并 / 下钻」的 placeCodes（可省略）
 *     · `dir:<base64url(目录键)>`      —— 该目录**直属**的照片
 *       （/api/photos?dirPath=）；根目录用保留 token `__root__`。
 *       ⚠️ 目录部分必须 base64url：目录名可能含 `:` 与 `@`，而它们正是本约定的
 *          分隔符（`encodeDirPayload` 的注释里有完整理由）。
 *   ⚠️ 没有 `scope` 时行为**一字不变**（全库列表 + 原提示文案）——
 *      待确认、概览、重复对比这些入口本来就不该改变翻页范围。
 *
 *   ⚠️ 首屏必须**带锚点**（`anchorPhotoCode=当前照片`）：这一批按 `takenAt DESC`
 *      分页，而从时间轴 / 人脸样本点进去的常常是一张**老照片**（某个人的 2000 多张
 *      里排在第 40 页往后）。只拉第 1 页的话当前照片不在列表里 ⇒ 两个箭头全禁用、
 *      键盘也不响应 —— 症状是「从人物库进来之后左右翻页坏了」，而且不报错。
 *      服务端算出它所在的那一页，窗口从那一页向两边按需长出去
 *      （见 `PhotoDetailView.loadScopePage` 的 `anchor` / `append` / `prepend`）。
 *
 * ⚠️ 判断「有没有上一页」依赖 vue-router 4 写在 `history.state` 里的 `back` 字段
 *    （`router.options.history.state` 公开，`back` 是其字段）。拿不到就**当没有**，
 *    退化成 `from` 兜底 —— 宁可少回退一次，也不能把用户原地弹回详情页；
 *    外站来源 `back` 的 origin 不同，被显式排除，避免一路退出本站。
 * ============================================================ */

/** 没有来源信息时的兜底：照片流 */
const PHOTO_FALLBACK = '/photos'

/** scope 允许的类型（白名单：值来自 URL，不能直接拼进请求） */
const SCOPE_TYPES = ['person', 'place', 'dir']

/**
 * 根目录的**保留 token** —— **必须与后端 `basicSettings.ALBUM_ROOT_TOKEN` 一致**。
 *
 * ⚠️ 为什么根目录不能直接用空串（本常量存在的唯一理由）
 * --------------------------------------------------
 *   两个地方都会**静默把空值丢掉**：
 *     ① `api/request.js` 的 `paramsSerializer` 会丢弃 `undefined` / `null` / `''`
 *        ⇒ 目录筛选参数根本不会发出 ⇒ 后端理解成"不筛选" ⇒ **返回全库照片**；
 *     ② 本文件的 `scopeParam()`（`code` 为空返回空串）与 `parseScope()`（`!code`
 *        返回 null）⇒ 作用域整个消失 ⇒ 详情页退化成"沿全库翻页"。
 *   两处的共同后果是「进了根目录，翻页却翻到全库」，而且**不报任何错**。
 *   ⇒ 三处（树端点的 `path`、照片列表的 `dirPath`、`scope=dir:` 的载荷）
 *     共用这一个 token，**不许再写第二份字面量**。
 */
export const ALBUM_ROOT_TOKEN = '__root__'

/**
 * 目录键 -> scope 载荷（base64url）。
 *
 * ⚠️ 为什么必须编码而不是直接把路径塞进 scope
 * ------------------------------------------
 *   scope 的格式是 `dir:<载荷>`，而载荷里可能出现 `:`（类型分隔符）与 `@`
 *   （extra 分隔符）—— 目录名完全可能包含它们（Windows 只禁 `< > : " / \ | ? *`，
 *   `@` 与中文都合法）。用百分号编码也不行：vue-router 会**再解码一次**，
 *   分隔符照样被还原出来。
 *   base64url 的字符集是 `A-Za-z0-9-_`，既不含分隔符，也不含会被 vue-router
 *   改写的内容 —— 这是唯一稳的选择。
 */
export function encodeDirPayload(dirKey) {
  const text = String(dirKey ?? '')
  if (!text) return ALBUM_ROOT_TOKEN
  const bytes = new TextEncoder().encode(text)
  let binary = ''
  bytes.forEach((one) => { binary += String.fromCharCode(one) })
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

/** scope 载荷 -> 目录键。解不出来一律当**根目录**（不抛错，免得整页白屏） */
export function decodeDirPayload(payload) {
  const text = String(payload ?? '')
  if (!text || text === ALBUM_ROOT_TOKEN) return ''
  try {
    const padded = text.replace(/-/g, '+').replace(/_/g, '/')
    const binary = atob(padded + '='.repeat((4 - (padded.length % 4)) % 4))
    const bytes = Uint8Array.from(binary, (one) => one.charCodeAt(0))
    return new TextDecoder().decode(bytes)
  } catch (err) {
    console.error('目录作用域载荷解析失败（手改过 URL？）', payload, err)
    return ''
  }
}

/**
 * 入口用：目录作用域（`dir:<base64url>`）。
 *
 * ⚠️ 不要自己写 `scopeParam('dir', dirKey)`：根目录的 `dirKey` 是空串，
 *   `scopeParam` 会因为 `code` 为空而返回**空串** ⇒ 作用域静默消失。
 *   必须经 `encodeDirPayload` 换成保留 token（那才是非空的）。
 */
export function dirScopeParam(dirKey) {
  return scopeParam('dir', encodeDirPayload(dirKey))
}

/**
 * 站点内路径的宽松校验：必须单个 `/` 开头。
 * ⚠️ 排除 `//evil.com` 这种协议相对写法 —— `from` 来自 URL，不能直接当跳转目标用。
 */
function safePath(value) {
  const path = String(value || '')
  return path.startsWith('/') && !path.startsWith('//') ? path : ''
}

/**
 * 入口用：生成照片详情的跳转目标（route object，可直接给 RouterLink 或 push）。
 *
 * @param {string} photoCode 照片编码
 * @param {{ path?: string, name?: string, scope?: string } | null} from 来源页：
 *   `path` 用 `route.fullPath`（带上筛选条件，兜底时才能还原）；
 *   `name` 用于按钮文案（人名 / 地点名 / 页面名）；
 *   `scope` 用 `scopeParam()` 生成，决定左右箭头翻哪一批照片
 */
export function photoDetailLink(photoCode, from = null) {
  const query = {}
  const path = safePath(from?.path)
  if (path) query.from = path
  const name = String(from?.name || '').trim()
  if (name) query.fromName = name
  const scope = String(from?.scope || '').trim()
  if (scope) query.scope = scope
  return { name: 'photoDetail', params: { photoCode: String(photoCode || '') }, query }
}

/**
 * 入口用：把「当前这一页」当作来源。
 * 子组件里同样可用 —— `useRoute()` 在 `router-view` 之内，拿到的就是宿主页。
 * 不传 name 时用路由 `meta.title`（「照片流」「待确认」这类页面名）。
 */
export function currentSource(route, name = '') {
  return { path: route.fullPath, name: name || String(route.meta?.title || '') }
}

/** 详情页用：同页跳转（「另一张照片」）时沿用当前来源**与翻页范围**，别丢 */
export function inheritSource(route) {
  return {
    path: route.query?.from,
    name: route.query?.fromName,
    scope: route.query?.scope,
  }
}

/**
 * 入口用：拼一个 scope 值；类型不认识或缺 code 就返回空串
 * （调用方直接塞进 `from.scope`，空串 = 不声明作用域 = 沿全库翻）。
 */
export function scopeParam(type, code, extra = '') {
  const t = String(type || '')
  const c = String(code || '')
  if (!SCOPE_TYPES.includes(t) || !c) return ''
  const e = String(extra || '')
  return e ? `${t}:${c}@${e}` : `${t}:${c}`
}

/** 详情页用：解析 scope 值；不认识就返回 null（当成「没有作用域」） */
export function parseScope(value) {
  const raw = String(value || '')
  const colon = raw.indexOf(':')
  if (colon <= 0) return null
  const type = raw.slice(0, colon)
  if (!SCOPE_TYPES.includes(type)) return null
  const rest = raw.slice(colon + 1)
  const at = rest.indexOf('@')
  const code = at >= 0 ? rest.slice(0, at) : rest
  const extra = at >= 0 ? rest.slice(at + 1) : ''
  if (!code) return null
  return { type, code, extra, key: raw }
}

/**
 * 详情页用：解析返回目标。
 * @returns {{ path: string, label: string }} `path` 是兜底跳转目标（可能含 query），
 *   `label` 是按钮文案
 */
export function photoReturnTarget(route) {
  const path = safePath(route.query?.from) || PHOTO_FALLBACK
  const fromName = String(route.query?.fromName || '').trim()
  let label = '返回上一页'
  if (fromName) label = `返回 ${fromName}`
  else if (path === PHOTO_FALLBACK) label = '返回照片流'
  return { path, label }
}

/** 历史栈里是否存在**站内**上一页 */
export function canGoBack() {
  const back = window.history?.state?.back
  if (!back) return false
  try {
    const from = new URL(String(back), window.location.origin)
    if (from.origin !== window.location.origin) return false
    // 站内但指向当前这条（replace 留下的痕迹）→ 当作没有上一页
    return from.pathname + from.search !== window.location.pathname + window.location.search
  } catch {
    return false
  }
}

/**
 * 执行返回（照片详情 / 人物详情 / 地点详情页头共用）。
 * 能回退就回退（与浏览器后退键同语义），没有站内上一页才去 `fallback`。
 *
 * @param {import('vue-router').Router} router
 * @param {string} fallback 兜底路径（可含 query），见各页的固定列表页
 * ⚠️ 兜底用**字符串**形式 replace：`{ path: '/photos?x=1' }` 不会解析 query，
 *    会把 `?x=1` 当成路径的一部分。
 */
export function goBackOr(router, fallback) {
  if (canGoBack()) {
    router.back()
    return
  }
  router.replace(String(fallback || PHOTO_FALLBACK))
}
