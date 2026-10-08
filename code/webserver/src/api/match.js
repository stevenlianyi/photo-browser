/* ============================================================
 * 人脸匹配接口薄封装（api/match.py）
 * ============================================================
 * ⚠️ 与 /facerec 不是一回事，别混：
 *   /facerec/**  从照片里**抽出**人脸（写 pb_face）——数量进「待确认」
 *   /match/**    把已抽出、尚无归属的人脸**判给某个人**（写 personCode）
 *                 ——机器认下的那些进「我不同意」
 *   前一步没跑，后一步就没有输入。
 *
 * ⚠️ 前缀是 /match 而不是 /face/*（**别改回去**）：/api/face/{faceCode} 是
 *   static.py 的人脸裁剪图路由，跨 router 时任何 /api/face/** 的静态段都可能
 *   被它接走并 404（api/face.py 文件头记过这一整轮教训）。
 */
import request from './request'

/**
 * 跑一次匹配（后台轻量线程，接口立刻返回）。
 *
 * assign=false 只算不写（预览）：结果先给用户看，确认后再以 assign=true 调一次
 * —— 两条路径的判定结果相同（幂等），区别只在「写不写库」。
 *
 * 后端会在**起线程之前**做完前置校验（质心为空 / 已有任务在跑 / 队列为空），
 * 失败会以 4xx/409 + 一句人话返回，所以调用方不必自己猜原因。
 */
export function runMatch(params = {}) {
  return request.post('/match/run', params)
}

/** 轮询阶段与真实计数（比对没有中间态，所以没有百分比；轮询不弹 Toast） */
export function getMatchStatus() {
  return request.get('/match/status', { silent: true })
}
