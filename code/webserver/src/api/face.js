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

/**
 * 识别**这一张**照片的人脸（单张；不建全库任务）。
 *
 * 为什么它不是 startFace 的一个参数
 * ------------------------------
 *   `/facerec/start` 是全库任务：取数走 `scanState = 0` 全表分页，没有
 *   photoCode 的概念 —— 想只认一张，只能把整个库认一遍。
 *   单张是秒级操作（首次要载模型 3~10s），所以它**不建任务、不用游标**，
 *   进度放在进程内快照里，轮询 /facerec/extract-one/status。
 *
 * ⚠️ 它**不阻塞**：接口立刻返回，真正的推理在后端线程里。
 *   前端必须轮询 —— 同步等会撞 axios 的 30s 超时，症状是
 *   「点了转很久，然后报超时」，而库里其实已经写好了。
 *
 * @param {object} params { photoCode, replaceFaces=true, match=true }
 *   replaceFaces: 重提取（先删这张照片的旧人脸行再插）。
 *     **会连带清掉这张照片上已有的人工确认与自动归属** —— 界面必须事前预告。
 *   match: 检测完顺便判「是谁」（**只判这张照片里的脸**，不动全库待确认队列）。
 *
 * 可能的错误（都带人话 message，照原样显示即可）：
 *   409 TASK_STATE_ILLEGAL  已有任务在跑（扫描 / 认脸 / 匹配），**不排队**
 *   400 PARAM_INVALID       缺依赖（onnxruntime/cv2）
 *   404 NOT_FOUND           照片不存在 / 原图不在磁盘上
 */
export function extractOneFace(params = {}) {
  return request.post('/facerec/extract-one', params)
}

/**
 * 单张识别的进度（轮询用；阶段是真的，不弹 Toast）。
 *
 * 返回里 `assignedTo` 是本次**自动归属**的结果
 * （[{faceCode, personCode, displayName, score}]）—— 自动归属是「我不同意」
 * 那一档（isConfirmed=0），界面上必须说得出它认了谁，否则用户只能看到一个
 * 「识别完成」而不知道机器做了什么。
 */
export function getExtractOneStatus() {
  return request.get('/facerec/extract-one/status', { silent: true })
}
