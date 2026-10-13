/* ============================================================
 * 年代档打包接口薄封装（api/eraZip.py · R15 · DR-56 ~ DR-58）
 * ============================================================
 * 为什么不放进 api/browse.js：browse.js 是**只读浏览**接口的封装
 * （它对应的后端模块文件头写着「一个字都不写」），而这里要**起后台任务**。
 * 也不放进 api/eraSample.js：那是「按年抽样」的命名空间（有批次、有随机种子），
 * 本模块是「按年代档全量、不落库、有进度轮询」—— 两者的状态模型完全不同。
 *
 * ⚠️ 与其它模块一样不做业务加工：参数名与后端逐字对齐。
 * ============================================================ */
import request, { API_BASE } from './request'

/**
 * 某个人的各年代档打包现状（张数 / 体积预估 / zip 是否已生成 / 是否过期）。
 *
 * ⚠️ 返回的 `photoTotal` 是**排除重复件后**的张数，会比 Tab1 时间轴写的
 *    张数小 —— 界面上必须把这两个数分开讲，否则用户以为包打少了。
 * @param {string} personCode
 */
export function listEraZips(personCode) {
  return request.get(`/persons/${encodeURIComponent(personCode)}/era-zips`)
}

/**
 * 起一次打包（**异步**，立刻返回 jobCode）。
 *
 * ⚠️ 已有产物且 `force=false` 时，后端**直接短路**返回
 *    `{ started: false, cached: true, downloadUrl }` —— 不起后台线程。
 *    所以调用方要判 `started` 再决定要不要轮询。
 *
 * @param {string} personCode
 * @param {string} bucketKey 后端档键（如 `2005-2009` / `NOYEAR`）
 * @param {boolean} force 已有产物也重打（原图补齐 / 归属变动后想重打时用）
 * @returns {Promise<{ok: boolean, started: boolean, cached: boolean,
 *                    jobCode: string|null, total: number, estBytes: number}>}
 */
export function startEraZip(personCode, bucketKey, force = false) {
  return request.post(
    `/persons/${encodeURIComponent(personCode)}/era-zips/${encodeURIComponent(bucketKey)}`,
    { force },
  )
}

/** 打包进度（按**张数**：processed/total）。jobCode 不是当前任务时返回 404。 */
export function getEraZipStatus(jobCode) {
  return request.get(`/era-zips/status/${encodeURIComponent(jobCode)}`)
}

/** 请求停止当前打包（在**张数边界**生效，不打断正在写的那一张）。 */
export function stopEraZip() {
  return request.post('/era-zips/stop')
}

/**
 * zip 的直接下载地址（给 `<a href>` / 导航用，**绕过 axios**）。
 *
 * ⚠️ 必须拼 `API_BASE` 而不是写死 `/api`：开发环境它是
 *    `http://127.0.0.1:8765/api`，写死会让下载跳到前端 dev server 上 404。
 * ⚠️ 文件名由**服务端**的 Content-Disposition 决定（RFC 5987 中文名
 *    `年代照片_张明_2005-2009.zip`），所以 `<a>` 上不要加 `download` 属性 ——
 *    加了会用 URL 末段（`download`）当文件名，中文名就白传了。
 */
export function eraZipDownloadUrl(personCode, bucketKey) {
  return `${API_BASE}/persons/${encodeURIComponent(personCode)}`
    + `/era-zips/${encodeURIComponent(bucketKey)}/download`
}
