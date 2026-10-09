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

/* ------------------------------------------------------------
 * 年代修正（DR-42）
 * ------------------------------------------------------------
 * 解决的是：老相册翻拍件 / 扫描件的 shotYear 是**翻拍那一刻**（EXIF / 文件名 /
 * mtime 三条兜底链给的都是它），于是这张照片里的人脸全落进错误的年代档。
 * 修正写的是 `pb_photo.shotYearOverride`（人工修正拍摄年），原值 shotYear 保留。
 *
 * 三条纪律：
 *   ① **两段式**：`getShotYearImpact` 是纯读预览；`fixShotYear(code, y, true)`
 *      才落库。预览必须显示 `persons[]` —— 改的是照片级年份，而年代档是
 *      f(年份, 各人出生年) 现算的，一张合影里不同生日的人会朝不同方向变。
 *   ② **"恢复自动" = 传 null**：显式 `{ shotYear: null }`。省略字段会得到 400
 *      （后端刻意用 exclude_unset 区分"漏传"与"恢复自动"，别把用户的修正
 *      因为一个漏传的字段静默清掉）。
 *   ③ 提交后会**重算质心**，所以别忘了刷新人物页/列表的缓存。
 */
export function getShotYearImpact(photoCode, shotYear) {
  const params = {}
  // ⚠️ null 表示「预览恢复自动」，此时**不能**带 shotYear 参数
  if (shotYear !== null && shotYear !== undefined) params.shotYear = shotYear
  return request.get(`/photos/${encodeURIComponent(photoCode)}/shot-year-fix`, {
    params,
  })
}

export function fixShotYear(photoCode, shotYear, confirm) {
  return request.post(
    `/photos/${encodeURIComponent(photoCode)}/shot-year-fix`,
    { shotYear: shotYear === undefined ? null : shotYear },
    { params: { confirm: confirm ? 1 : 0 } },
  )
}

/* ------------------------------------------------------------
 * 人工旋转（DR-43）
 * ------------------------------------------------------------
 * 只写 `pb_photo.rotateDeg`（0/90/180/270，顺时针为正）—— **原图一个字节都不动**，
 * 缩略图缓存不失效，`/api/original` 的 Range 直传不受影响（没有服务端转码）。
 *
 * 三条纪律：
 *   ① **不需要 confirm**：旋转不改变任何识别事实（bbox / 归属 / 质心 / 年代档 /
 *      faceCount 全不动），没有"必须先让用户看见的影响面"，而且完全可逆
 *      （再转回去即可）。所以这里是一步到位的写，不弹影响面。
 *   ② **不落 pb_review_log**：那条链只装归属事实，旋转是纯显示属性。
 *   ③ **只传 0/90/180/270**：角度累加在前端做 `(cur + delta + 360) % 360`，
 *      后端不做 `% 360` 归约（传 45 / -90 / "abc" 一律 400 —— 静默归约会把
 *      "前端角度算错了"变成看不见的 bug）。
 *   ⚠️ 下载原图得到的**永远是原始方向**（原图不动）。这是有意为之，
 *      不要顺手给下载链接加旋转 —— 那属于「旋转后另存」的导出功能，另开一步。
 */
export function rotatePhoto(photoCode, rotateDeg) {
  return request.post(`/photos/${encodeURIComponent(photoCode)}/rotate`, { rotateDeg })
}

export function resetRotate(photoCode) {
  return request.post(`/photos/${encodeURIComponent(photoCode)}/rotate-reset`)
}
