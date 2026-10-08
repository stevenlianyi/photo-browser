/* ============================================================
 * 人脸识别任务接口薄封装（api/face.py）
 * ============================================================
 * ⚠️⚠️ 端点前缀是 /facerec 而不是 /face —— **别改回去**
 * -------------------------------------------------------
 *   `/api/face/{faceCode}` 是 static.py 里「人脸裁剪图」的路由，
 *   而 staticApi 是第一个 include 的 router。FastAPI 0.142 把
 *   include_router 的子路由包成 `_IncludedRouter` 之后，跨 router 的
 *   匹配不再保证「静态段优先于参数段」，于是 `/api/face/jobs` 会先落进
 *   `{faceCode}`、被当成 faceCode 去查脸、查不到就 404。
 *   而统一错误处理对**任何** 404 都写成「接口或资源不存在: <路径>」，
 *   于是现象是「路由明明在 openapi 里却一直 404」，极难定位。
 *   ⇒ 任务类端点一律走 /facerec/**，与 {faceCode} 彻底分开。
 *
 * 与 scan.js 的分工
 * ----------------
 *   扫描（步骤 3）把照片写进 pb_photo；人脸识别（步骤 5）把特征写进 pb_face。
 *   **只有后者会产生「待确认」** —— 侧栏角标数的是 pb_face 里
 *   personCode IS NULL 的行，与 pb_photo 一点关系都没有。
 *   所以「扫了多少张」与「有多少张待认脸」必须能在界面上分开看到，
 *   否则就会出现「扫了 1000 张、待确认 0 张、但看不出哪里不对」——
 *   本项目已经在这个坑上浪费过一整轮扫描。
 */
import request from './request'

/** 开始人脸识别（后台线程，接口立刻返回 jobCode）
 *  replaceFaces: true = 重提取（先删旧人脸行）。日常增量跑不要开 ——
 *  它会连带丢掉已人工确认的结果（pb_face 的 upsert 刻意不刷 isConfirmed） */
export function startFace(params = {}) {
  return request.post('/facerec/start', params)
}

/** 轮询进度（真实计数；轮询不弹 Toast） */
export function getFaceStatus(jobCode) {
  return request.get(`/facerec/status/${encodeURIComponent(jobCode)}`, { silent: true })
}

/** 停止（wait=1 等到后台线程真的退出再返回） */
export function stopFace(jobCode, params = {}) {
  return request.post(`/facerec/stop/${encodeURIComponent(jobCode)}`, null, { params })
}

/** 人脸识别任务列表（分页 + 按状态过滤） */
export function listFaceJobs(params = {}) {
  return request.get('/facerec/jobs', { params })
}

/**
 * 还剩多少张没认脸 + 环境自检（不建任务）。
 * `environment.ok === false` 时**不要**去点开始：缺 onnxruntime/cv2 会让
 * 任务「成功」却检出 0 张脸（子进程 import 就死），提示必须先摆出来。
 */
export function getFacePendingCount() {
  return request.get('/facerec/pending-count', { silent: true })
}
