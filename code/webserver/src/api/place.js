/* ============================================================
 * 地点接口薄封装（api/place.py）+ 地点显示名的**唯一入口**
 * ============================================================
 * 只做「路径 + 参数名」的转写，不做业务加工 —— 参数名与后端 Query 逐字对齐。
 *
 * ⚠️ `placeDisplayName()` 为什么放在这里而不是 utils/
 *    · 它与 `api/static.js` 的 `thumbUrl()` / `faceUrl()` 是同一类东西：
 *      把「后端给的字段」翻译成「界面要用的字符串」，与请求同源同文件；
 *    · 更重要的是它必须**只有一个定义**（见下）。
 */
import request from './request'

/** 中文显示名的字段：地点接口叫 `nameZh`，照片接口叫 `placeZh`（R4a 的历史） */
function zhNameOf(place) {
  return String(place?.nameZh || place?.placeZh || '').trim()
}

/**
 * 地点显示名 —— **全站唯一入口**，所有视图都调它，不要各写各的。
 *
 * 契约（后端三处注释同时写明，改一处就会三处不一致）：
 *   显示名 = `nameZh || placeName || '未知地点'`
 *
 * | 地点类型            | placeName                     | nameZh            | 显示     |
 * |---------------------|-------------------------------|-------------------|----------|
 * | GPS 地点（17 个）    | `CN, Beijing, Datun`（英文）   | `北京市 · 朝阳区`  | **中文** |
 * | 目录名地点（11 个）  | `华盛顿`（**已是中文**）        | `NULL`            | 华盛顿   |
 * | 幽灵行 photoCount=0 | `GH, Western, Takoradi`       | `NULL`            | 不展示   |
 *
 * ⚠️ 两列都**不能少**：
 *   · 只看 `nameZh` -> 11 个目录名地点（90% 的照片）全变成「未知地点」；
 *   · 只看 `placeName` -> GPS 地点永远是英文，这正是 R4a 要解决的问题。
 * ⚠️ 也不要把英文 `placeName` 丢掉：排障时「这个中文名是从哪个英文键算出来的」
 *   只能靠它（后端刻意**不用** placeZh 覆盖 placeName 返回，正是为此）。
 *
 * ⚠️ 顺带认 `placeZh` 作为 `nameZh` 的别名（**不是**为了宽松，而是为了
 *    让调用方不必在每处都写一遍转写）：照片接口（`photoSummary` /
 *    `getPhoto`）给的是 `placeZh`，地点接口给的是 `nameZh`，两个名字同一语义。
 *    不在这一处收掉的话，每个用到照片列表的页面都要写一次
 *    `{ nameZh: p.placeZh, placeName: p.placeName }` —— 少写一处，
 *    那一处的地点就会显示成英文，而它看起来完全正常。
 */
export function placeDisplayName(place) {
  const nameZh = zhNameOf(place)
  if (nameZh) return nameZh
  const name = String(place?.placeName || '').trim()
  return name || '未知地点'
}

/** 英文原值（排障 tooltip 用）。没有、或与显示名相同时返回空串。 */
export function placeRawName(place) {
  const raw = String(place?.placeName || '').trim()
  // 显示名与英文原值相同时不必重复显示（目录名地点就是这样）
  return raw && raw !== placeDisplayName(place) ? raw : ''
}

/** 地点列表（分页 + 筛选）。`groupByNameZh=1` 时按中文名归并同名地点。 */
export function listPlaces(params = {}) {
  return request.get('/places', { params })
}

/** 地点详情（张数 / 年份跨度 / 坐标 / 同组下钻 placeCodes + members） */
export function getPlace(placeCode, params = {}) {
  return request.get(`/places/${encodeURIComponent(placeCode)}`, { params })
}

/** 该地点的照片（分页；years[] 是整个地点的按年计数） */
export function listPlacePhotos(placeCode, params = {}) {
  return request.get(`/places/${encodeURIComponent(placeCode)}/photos`, { params })
}

/** 出现在该地点的人（实时 join，不落库；空的时候带出 photoTotal 供引导文案） */
export function listPlacePersons(placeCode, params = {}) {
  return request.get(`/places/${encodeURIComponent(placeCode)}/persons`, { params })
}

/**
 * **这个人**去过的地方（P-05 Tab1 时间轴下方的区块）。
 *
 * ⚠️ 端点在 `api/browse.py`（与 `/persons/{code}/timeline`、`/faces` 同模块，
 *    它们是一组兄弟端点），**不是** `/places/*`。放在本文件只是因为它
 *    属于「地点」这条业务线 —— 路径与模块的对应关系写在注释里，
 *    免得下一个人去 place.py 里找它。
 * 返回里除 `places[]` 还有 `photoTotal` / `locatedPhotoTotal`（空状态文案要用）。
 */
export function getPersonPlaces(personCode, params = {}) {
  return request.get(`/persons/${encodeURIComponent(personCode)}/places`, { params })
}

/** 重建地点字典（幂等；必须 POST，用 GET 会拿到 405 METHOD_NOT_ALLOWED） */
export function rebuildPlaces() {
  return request.post('/places/rebuild')
}
