/* ============================================================
 * 纠错 / 待确认接口薄封装（api/review.py）
 * ============================================================
 * 两条队列口径**不可混用**（设计稿 §4.6 / DR-16）：
 *   · 待确认   personCode IS NULL AND isStranger=0     —— 没归属的脸
 *   · 我不同意 personCode IS NOT NULL AND isConfirmed=0 —— 机器自动认的
 *     （用户浏览时最可能发现认错的那批）。两者要处理的问题不同，
 *     侧栏角标必须分两个数字，不能合并成一个。
 *
 * 改判是**唯一**的纠错入口：fix 覆盖「改判到某人 / 置为未知 / 标记陌生人」，
 * 服务端负责删旧 linkKey + 写新 source=1 + 重算原人与新人全部桶 + 落 pb_review_log。
 */
import request from './request'

/** 待确认队列（含 Top-5 候选，按相似度降序） */
export function getPending(params = {}) {
  return request.get('/review/pending', { params })
}

/** 「我不同意」列表：按照片分组，支持整张否决 */
export function getDisputed(params = {}) {
  return request.get('/review/disputed', { params })
}

/** 侧栏角标：**必须返回两个数**（pendingCount / disputedCount），不合并 */
export function getPendingCount() {
  return request.get('/review/pending/count', { silent: true })
}

/** 待确认集合的簇概览（批量改判入口） */
export function getClusters(params = {}) {
  return request.get('/review/clusters', { params })
}

/** 某个簇的活成员（现查不缓存，成员随确认与否实时变化） */
export function getClusterMembers(clusterCode, params = {}) {
  return request.get(`/review/clusters/${encodeURIComponent(clusterCode)}`, { params })
}

/** 首次人工确认：isConfirmed=1 + link source=1 + 立即重算质心 */
export function assignFace(faceCode, body) {
  return request.put(`/review/${encodeURIComponent(faceCode)}/assign`, body)
}

/** 批量确认（同一人的一批脸：一个事务 + 一次重算 + 一条日志） */
export function batchAssign(body) {
  return request.post('/review/batch-assign', body)
}

/**
 * 改判（统一入口）
 * action ∈ assign（改判到某人）/ unknown（置为未知）/ stranger（标记陌生人）
 * ⚠️ stranger 永久排除该脸，前端必须二次确认
 */
export function fixFace(body) {
  return request.post('/review/fix', body)
}

/** 同一簇批量改判（一次点击解决 N 张） */
export function batchFix(body) {
  return request.post('/review/batch-fix', body)
}

/** 合并两个人物档案（不可逆，但可撤销） */
export function mergePersons(body) {
  return request.post('/review/merge', body)
}

/** 从某人拆出一张脸 */
export function splitFace(body) {
  return request.post('/review/split', body)
}

/** 撤销一条 SPLIT / MERGE（唯一的逆操作入口） */
export function undoOperation(body) {
  return request.post('/review/undo', body)
}

/** 当前可撤销的操作列表（「撤销上次合并」按钮的候选集） */
export function getRevertible(params = {}) {
  return request.get('/review/revertible', { params })
}

/** 纠错操作历史（这张脸当初怎么被认成这个人的） */
export function getReviewLog(params = {}) {
  return request.get('/review/log', { params })
}