/* ============================================================
 * 扫描任务接口薄封装（api/scan.py）
 * ============================================================
 * 关键语义（设计稿 §4.7 / §6.1）：
 *   · 扫描是**后台任务**，start 立刻返回 jobCode，进度靠 status 轮询，
 *     六个计数全部来自 pb_scan_job 行，不是内存里的估算（P0-4 进度诚实）。
 *   · **批次限流**：每处理 batchSize（默认 100）张即置 PAUSED，
 *     等人工点「继续下一批」，不是跑到底。
 *   · stop 之后调度层映射成 PAUSED（不是 DONE —— 那会让「扫完了」
 *     与「一张没扫」变成同一个状态）。
 *   · **autoFace**（缺省 true）：扫描跑完自动接人脸识别。
 *     扫描写的是 pb_photo，而「待确认」数的是 pb_face ——
 *     不识别人脸，扫一万张待确认也永远是 0，且界面上看不出任何异常。
 */
import request from './request'

/** 开始扫描（后台线程，接口立刻返回 jobCode）
 *  rootPath 必须等于后端 photo_dir()，库外目录与子目录都会被 400 挡掉
 *  batchSize 每批张数，默认 100
 *  autoFace 扫描跑完是否自动识别人脸（默认 true） */
export function startScan(rootPath, batchSize = 100, autoFace = true) {
  return request.post('/scan/start', { rootPath, batchSize, autoFace })
}

/** 轮询进度：已处理 / 本批 / 累计 / 状态 / 剩余（真实计数；轮询不弹 Toast） */
export function getScanStatus(jobCode) {
  return request.get(`/scan/status/${encodeURIComponent(jobCode)}`, { silent: true })
}

/** 继续下一批（wait=1 时同步跑完一批再返回，点一下就能看到结果） */
export function resumeScan(jobCode, params = {}) {
  return request.post(`/scan/resume/${encodeURIComponent(jobCode)}`, null, { params })
}

/** 停止扫描（wait=1 等到后台线程真的退出再返回） */
export function stopScan(jobCode, params = {}) {
  return request.post(`/scan/stop/${encodeURIComponent(jobCode)}`, null, { params })
}

/** 任务列表（分页 + 按状态过滤） */
export function listJobs(params = {}) {
  return request.get('/scan/jobs', { params })
}

/** 预览 jobCode 格式（不建任务、不写一行；新建扫描对话框做输入提示用） */
export function getScanCodePreview() {
  return request.get('/scan/code-preview')
}