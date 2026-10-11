/* ============================================================
 * 年代抽样接口薄封装（api/eraSample.py · R11）
 * ============================================================
 * 为什么不放进 api/browse.js：browse.js 是**只读**浏览接口的封装
 * （它对应的后端模块文件头写着「一个字都不写」）。本模块有 3 个写接口
 * （生成 / 打包 / 删除），混进去会让「浏览=只读」这条心智模型失效。
 *
 * ⚠️ 与其它模块一样不做业务加工：参数名与后端逐字对齐。
 * ============================================================ */
import request, { API_BASE } from './request'

/**
 * 生成一次抽样（**写批次 + 明细**）。
 *
 * ⚠️ **每次调用都新建一个批次**（不覆盖旧批次）：加权随机本来就允许
 * 「这次不满意，再抽一次」。历史批次可回看、可下载。
 *
 * @param {string} personCode
 * @param {object} body grain / mode / seed / yearFrom / yearTo /
 *                      includeNoYear / onlyConfirmed / excludeDuplicate / note
 * @returns {Promise<{ok: boolean, batch: object, items: object[]}>}
 */
export function createEraSample(personCode, body = {}) {
  return request.post(`/persons/${encodeURIComponent(personCode)}/era-sample`, body)
}

/** 某个人的历史批次（最新一次在最前，**不含明细**） */
export function listEraSamples(personCode, params = {}) {
  return request.get(`/persons/${encodeURIComponent(personCode)}/era-samples`, { params })
}

/** 某批次的头 + 按年份排队的明细 */
export function getEraSample(batchCode) {
  return request.get(`/era-samples/${encodeURIComponent(batchCode)}`)
}

/**
 * 打包 zip（幂等：已有产物直接复用）。
 *
 * ⚠️ 先用它再下载（而不是直接导航到下载地址）：只有它的响应里带
 *    `zipMissingCount` / `missing[]` —— 原图不在磁盘上时用户必须被告知
 *    「少了 2 张」，否则他拿到一个安静地少了几张照片的 zip。
 */
export function buildEraSampleZip(batchCode, force = false) {
  return request.post(`/era-samples/${encodeURIComponent(batchCode)}/zip`, { force })
}

/**
 * zip 的直接下载地址（给 `<a href>` / 导航用，**绕过 axios**）。
 *
 * ⚠️ 必须拼 `API_BASE` 而不是写死 `/api`：开发环境它是
 *    `http://127.0.0.1:8765/api`，写死会让下载跳到前端 dev server 上 404。
 * ⚠️ 文件名由**服务端**的 Content-Disposition 决定（RFC 5987 中文名），
 *    所以 `<a>` 上不要加 `download` 属性 —— 加了会用 URL 末段
 *    （`<batchCode>.zip`，一串 ASCII）当文件名，中文名就白传了。
 */
export function eraSampleZipUrl(batchCode) {
  return `${API_BASE}/era-samples/${encodeURIComponent(batchCode)}/zip`
}
