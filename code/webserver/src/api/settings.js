/* ============================================================
 * 设置接口薄封装（api/settings.py）
 * ============================================================
 * 两条必须让界面说清楚的语义
 * --------------------------
 * 1. **参数改动是进程内的**：不写回配置文件，重启服务即回到 basicSettings。
 *    界面上必须写明，否则用户重启后发现阈值变了回去，会当成 bug。
 * 2. **改参数 ≠ 已重算质心**：POST /settings/match 只改开关并返回
 *    needRebucket / needCentroidRebuild。要真重算得再调 /centroids/rebuild。
 *    两者合成一步看着方便，但「参数改了」与「数据重算了」在界面上必须是
 *    两个可分别确认的状态 —— 尤其分钟级的全库重算不该被一次点击顺带触发。
 *
 * 备份/恢复**不在这里**：备份的定义是「停服务 → 拷贝 db + thumb」，
 * 而服务自己开着的时候拷走的是 WAL 中间态（恢复后少数据且不报错）。
 * 所以只有只读的清单接口 + 可照抄的命令行，见 api/settings.js 底部。
 */
import request from './request'

/** 设置页首屏一次给全：三条路径 + 识别参数 + 质心现状 + 备份清单 */
export function getSettings() {
  return request.get('/settings')
}

/**
 * 改识别参数。都不给 = 全部复位回配置值。
 * @param {{tLow?:number,tHigh?:number,preset?:string,bucketStrategy?:string,confirmedOnly?:boolean}} body
 * 响应里的 needRebucket / needCentroidRebuild / steps 决定界面上怎么提示
 */
export function updateMatch(body) {
  return request.post('/settings/match', body || {})
}

/** 重新生成质心（后台任务，立刻返回；进度用轮询）
 *  personCode 省略 = 全库；rebucketFirst 默认 true（DR-22 的硬顺序，别关） */
export function rebuildCentroids(body) {
  return request.post('/settings/centroids/rebuild', body || {})
}

/** 质心重算进度（真实计数；轮询用 silent，别每 2 秒弹一次 Toast） */
export function getRebuildStatus() {
  return request.get('/settings/centroids/status', { silent: true })
}

/** 已有备份清单（只读）+ 可照抄的命令行 */
export function listBackups() {
  return request.get('/settings/backups')
}