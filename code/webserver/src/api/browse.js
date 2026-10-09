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
 *       keyword / orderBy / desc / anchorPhotoCode
 *  mode 是多人语义：or=任一 / and=合影
 *  ⚠️ `anchorPhotoCode` 是**定位**不是筛选：给了它且这张照片在筛选结果里时，
 *     返回的是**它所在的那一页**（响应 page 是算出来的）。照片详情页从人物 /
 *     地点详情进来（`?scope=`）时用它落到正确的一页 —— 不带锚点的话，
 *     一张排在几十页之后的老照片会让左右箭头全禁用（见 PhotoDetailView
 *     `loadScopePage` 的 'anchor' 分支）。 */
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

/** 人物详情：含各年代档分组统计（P-05 时间轴） */
export function getPerson(personCode) {
  return request.get(`/persons/${encodeURIComponent(personCode)}`)
}

/** 某人的全部人脸样本（P-05 Tab2）
 *  state = confirmed | disputed | pending | stranger（不给则四态全给）
 *  ⚠️ counts 是**两段各自的真实条数**，界面上「人工确认 N / 自动 M」取这里，
 *     不要取 items.length —— 有 limit 时两个数会不一样。 */
export function listPersonFaces(personCode, params = {}) {
  return request.get(`/persons/${encodeURIComponent(personCode)}/faces`, { params })
}

/** 某人的照片按年代档分组（P-05 Tab1 时间轴；年代档键就是 pb_face.shotBucket）
 *  每个年代档只给 limitPerBucket 张 + photoTotal，其余由 BucketTimeline 折成「+N」 */
export function getPersonTimeline(personCode, params = {}) {
  return request.get(`/persons/${encodeURIComponent(personCode)}/timeline`, { params })
}

/** 重复照片分组（按内容指纹聚合）
 *  kind=copy 是「复制了一份」，kind=moved 是「这其实是被改过名的同一张」——
 *  两者在库里长得一样（isDuplicate=1），混着说会让用户去删错文件。 */
export function listDuplicates(params = {}) {
  return request.get('/duplicates', { params })
}

/** 两张重复照片的并排对比：sameContent + 逐项 diff + 人脸配对 */
export function compareDuplicates(photoCode, otherCode) {
  return request.get('/duplicates/compare', {
    params: { photoCode, otherCode },
  })
}

/* ⚠️ R5：`/api/places*` 的封装**整套搬到了 `api/place.js`**（含显示名契约
 *    `placeDisplayName`）。两个模块各写一份请求封装的后果不是"重复一点代码"，
 *   而是「改了一处、另一处静默过期」——本文件里再留一份，它迟早会与
 *    place.js 的参数名/默认值漂开，而两边都还能跑。
 *    （后端同理：`/api/places*` 整个命名空间只在 `api/place.py` 一处注册。） */