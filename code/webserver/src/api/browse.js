/* 浏览类接口薄封装（api/browse.py）
 * 只做「路径 + 参数名」的转写，不做业务加工 —— 参数名与后端 Query 逐字对齐。
 * 步骤 10 不接数据：函数已就位，由 store 调用，页面暂不自动触发。 */
import request from './request'

/** 一次给全首屏计数：照片数 / 人物数 / 待确认数 / 最近扫描 */
export function getOverview() {
  return request.get('/overview')
}

/** 时间线：按年月分组分段拉取（10 万张不用深 OFFSET） */
export function getTimeline(params = {}) {
  return request.get('/timeline', { params })
}

/** 照片列表（照片流网格 / 时间轴共用）
 *  参数：page / size / personCode / personCodes / mode / shotYearFrom /
 *       shotYearTo / placeName / hasFace / isDuplicate / isMissing /
 *       keyword / orderBy / desc
 *  mode 是多人语义：or=任一 / and=合影 */
export function listPhotos(params = {}) {
  return request.get('/photos', { params })
}

/** 照片详情：EXIF + 出现的人 + 人脸归属状态（P-03 侧栏的全部数据） */
export function getPhoto(photoCode) {
  return request.get(`/photos/${encodeURIComponent(photoCode)}`)
}

/** 人物网格：photoCount / faceCount / 年代跨度 */
export function listPersons(params = {}) {
  return request.get('/persons', { params })
}

/** 人物详情：含各年代桶分组统计（P-05 时间轴） */
export function getPerson(personCode) {
  return request.get(`/persons/${encodeURIComponent(personCode)}`)
}

/** 地点聚合（筛选下拉 / 后续离线地图） */
export function listPlaces(params = {}) {
  return request.get('/places', { params })
}

/** 重建地点字典（幂等；必须 POST，用 GET 会拿到 405 METHOD_NOT_ALLOWED） */
export function rebuildPlaces() {
  return request.post('/places/rebuild')
}