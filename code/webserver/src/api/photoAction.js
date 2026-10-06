/* ============================================================
 * 照片级写接口（api/photoAction.py，步骤 11）
 * ============================================================
 * 四条纪律（前端必须配合，缺一条就会出事）：
 *   ① **软删除是两段式**：`getDeleteImpact` 是**纯读**（一行都不写），
 *      用来先把后果说清楚；`softDelete(code, true)` 才真的执行。
 *      界面上必须先弹影响面、再让用户确认 —— 不可逆语义的动作不能一键到底。
 *   ② **原图零风险**：这三个动作只改数据库。磁盘上的 photo\ 目录一个字节都不动，
 *      也不改名、不覆盖。UI 不提供任何编辑/覆盖/删除原图的入口。
 *   ③ **重复标记必填目标**（dupOfPhotoCode）：「这条记录重复了」是一句无法核对、
 *      也无法撤销的话，所以必须指向一张具体的主照片。
 *   ④ 恢复是**级联**的：照片回来时它的人脸也一起回来（否则脸会变成
 *      「既不在待确认也不在我不同意」的盲区）。
 */
import request from './request'

export function getDeleteImpact(photoCode) {
  return request.get(`/photos/${encodeURIComponent(photoCode)}/delete-impact`)
}

export function softDelete(photoCode, confirm) {
  return request.post(`/photos/${encodeURIComponent(photoCode)}/soft-delete`, null, {
    params: { confirm: confirm ? 1 : 0 },
  })
}

export function restorePhoto(photoCode) {
  return request.post(`/photos/${encodeURIComponent(photoCode)}/restore`)
}

export function markDuplicate(photoCode, dupOfPhotoCode) {
  return request.post(`/photos/${encodeURIComponent(photoCode)}/mark-duplicate`, {
    dupOfPhotoCode,
  })
}

export function unmarkDuplicate(photoCode) {
  return request.post(`/photos/${encodeURIComponent(photoCode)}/unmark-duplicate`)
}
