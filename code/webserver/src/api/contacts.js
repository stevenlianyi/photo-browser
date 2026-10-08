/* ============================================================
 * 联系人 / 家庭组接口薄封装（api/contacts.py）
 * ============================================================
 * 三条硬纪律（DR-17/18/19），前端必须配合：
 *   1. **不提供任何导出接口** —— 迁移与备份只靠设置页的整库拷贝。
 *   2. **只停用、不删除**（delFlag=1）—— 一个误点不该把几小时的确认工作清零；
 *      停用前必须先拉 /impact 把影响面说清楚，再带 confirm=true 才执行。
 *   3. 改 birthday 会重算质心（响应带 centroidRebuilt=true），
 *      改其它字段不会 —— Toast 只在 birthday 真的变了时弹，别给无影响的改动
 *      也弹「质心已重算」，那是噪声。
 */
import request from './request'

/** 联系人列表（分页 + 关键词 / 分类 / 家庭组 / 已停用 筛选） */
export function listContacts(params = {}) {
  return request.get('/contacts', { params })
}

/** 界面新建手工档案（source=0、vcardUid 空） */
export function createContact(body) {
  return request.post('/contacts', body)
}

/** 部分字段更新（PATCH 语义：只写传了的字段）
 *  撞 displayName（UNIQUE）→ 409 + { code: DUPLICATE_DISPLAY_NAME, existing } */
export function patchContact(personCode, body) {
  return request.patch(`/contacts/${encodeURIComponent(personCode)}`, body)
}

/** 设 / 清某个人的**默认头像**（DR-41，走 PATCH 的部分字段语义）。
 *
 *  faceCode 给空串 = **清空**（卡片与详情回退到服务端算好的代表脸，DR-40）。
 *  ⚠️ 头像只影响**展示**：不进质心、不改 pb_face、不写 review 日志。
 *  ⚠️ 传**别人的** faceCode 后端回 400（不允许库里出现「头像不属于他」的行）。 */
export function setPersonAvatar(personCode, faceCode) {
  return patchContact(personCode, { avatarFaceCode: String(faceCode || '') })
}

/** 停用影响面预览（**只读，一行都不写**） */
export function getImpact(personCode) {
  return request.get(`/contacts/${encodeURIComponent(personCode)}/impact`)
}

/** 停用。confirm=1 才执行；不给 / 0 只返回影响面 */
export function disableContact(personCode, confirm) {
  return request.post(`/contacts/${encodeURIComponent(personCode)}/disable`, null, {
    params: { confirm: confirm ? 1 : 0 },
  })
}

/** 恢复（响应带 note：需重新确认人脸才能自动匹配） */
export function enableContact(personCode) {
  return request.post(`/contacts/${encodeURIComponent(personCode)}/enable`)
}

/** 疑似同人合并候选（同名归一 / 同邮箱 / 同电话），不是「两个同名人员」 */
export function listDuplicates(params = {}) {
  return request.get('/contacts/duplicates', { params })
}

/** 上传 CSV 导入联系人（multipart；dryRun=1 只回计划不写库） */
export function importCsv(file, dryRun = false) {
  const form = new FormData()
  form.append('file', file)
  return request.post('/contacts/import/csv', form, {
    params: { dryRun: dryRun ? 1 : 0 },
    headers: { 'Content-Type': 'multipart/form-data' },
  })
}

/** 家庭组列表 / 详情 / 新建 / 改名（P2，可延后） */
export function listFamilies(params = {}) {
  return request.get('/families', { params })
}

export function getFamily(familyCode) {
  return request.get(`/families/${encodeURIComponent(familyCode)}`)
}

export function createFamily(body) {
  return request.post('/families', body)
}

export function patchFamily(familyCode, body) {
  return request.patch(`/families/${encodeURIComponent(familyCode)}`, body)
}