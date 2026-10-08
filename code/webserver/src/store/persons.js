/* ============================================================
 * 人物 store —— 名册 / 筛选 / 单人档案
 * ============================================================
 * 筛选与后端 /api/persons 的 Query 对应（api/browse.js）。
 * 分类取值：family 家人 / friend 朋友 / colleague 同事；
 * delFlag 是「停用」不是「删除」（DR-19：只停用不删除）。
 *
 * ⚠️ 空 query 参数不用在 store 里逐个 `|| undefined` ——
 *    api/request.js 的 paramsSerializer 已根治（只丢 undefined/null/''，
 *    0 与 false 原样发出，因为 desc=0 与「不传 desc」是两个意思）。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { getPerson, getPersonTimeline, listPersonFaces, listPersons } from '@/api/browse'
import {
  createFamily as createFamilyApi,
  listFamilies,
  setPersonAvatar,
} from '@/api/contacts'
import { getPersonPlaces } from '@/api/place'

export const PERSON_CATEGORIES = [
  { value: 'family', label: '家人' },
  { value: 'friend', label: '朋友' },
  { value: 'colleague', label: '同事' },
]

/**
 * 分类值 -> 中文标签。
 *
 * ⚠️ 库里存的是英文码（`family`/`friend`/`colleague`，步骤 8 从 Outlook 的
 *    `Categories` 列拆出来就是这个），界面显示中文。
 * 未知值**原样返回**而不是吞成空串：联系人里可能有「同事:|同事:同事」这类
 * 自己写的分类，界面上要让人看见自己写了什么，而不是显示一片空白。
 */
export function categoryLabelOf(value) {
  const text = String(value || '').trim()
  const found = PERSON_CATEGORIES.find((item) => item.value === text)
  return found ? found.label : text
}

/**
 * 家庭关系（`pb_person.relation`）：**值同样是英文码，界面显示中文**。
 *
 * 这里是**唯一一份**关系表：`PersonForm` 的下拉、人物卡片、人物详情头部、
 * 改判候选的「注意区分」提示全部读它。分成两处的话，
 * 下拉里选了「配偶」，卡片上会显示 `spouse` —— 用户根本看不出刚填的生效了没有。
 *
 * ⚠️ `colleague` 不在后端 `RELATION_ALL`（`normalizeRelation` 只认
 *    parent/spouse/child/sibling，导入侧认不出来的一律丢弃），但界面**可以**手选，
 *    所以 PATCH 之后库里真的会存这个值 —— 表里必须留着它。
 * 未知值原样返回（与 categoryLabelOf 同款口径）：宁可让人看见原始值，
 * 也不要显示成一片空白。
 */
export const PERSON_RELATIONS = [
  { value: 'parent', label: '父亲 / 母亲' },
  { value: 'spouse', label: '配偶' },
  { value: 'child', label: '子女' },
  { value: 'sibling', label: '兄弟姐妹' },
  { value: 'colleague', label: '同事' },
]

/** 关系值 -> 中文标签（未知值原样返回） */
export function relationLabelOf(value) {
  const text = String(value || '').trim()
  const found = PERSON_RELATIONS.find((item) => item.value === text)
  return found ? found.label : text
}

/**
 * 质心健康度（DR-16 硬约束）
 * ---------------------------------------------------------
 * 「自动归属 ≫ 人工确认」= 识别质量偏低。
 * 为什么值得专门提示：自动归属的那批脸**同时是「我不同意」列表的来源**，
 * 而用户往往先在照片流里逛几个月才想起去处理队列 —— 等他自己发现
 * 「有个人越看越不像」早就晚了。用数据把人推到纠错入口，是唯一有效的做法。
 *
 * 判定用**比例**而不是绝对差：
 *  · 确认样本为 0 且有脸 -> 他**不参与自动匹配**（DR-16③），质心根本不存在；
 *  · 自动 >= 确认 × 3   -> 提示复核（阈值与设计稿 §4.8 的 `⚠偏低` 一致）
 */
const AUTO_OVER_CONFIRMED_RATIO = 3

export function healthOf(person) {
  if (!person) return null
  const confirmed = Number(person.confirmedFaceCount) || 0
  const auto = Number(person.autoFaceCount) || 0
  const faceCount = Number(person.faceCount) || 0
  if (faceCount === 0) return null
  if (confirmed === 0) {
    return {
      key: 'noConfirmed',
      label: '确认样本为 0',
      tone: 'warning',
      hint: '没有人工确认样本 ⇒ 该人的质心不存在、不参与自动匹配。'
        + '先在待确认队列里认几张，这里才会变准。',
    }
  }
  if (auto >= confirmed * AUTO_OVER_CONFIRMED_RATIO) {
    return {
      key: 'autoHeavy',
      label: '识别质量偏低',
      tone: 'warning',
      hint: `自动归属 ${auto} 张 ≫ 人工确认 ${confirmed} 张，`
        + `建议人工确认 ${Math.min(auto, Math.max(1, auto - confirmed))} 张 —— `
        + '自动认错的那张脸会把质心拉偏，越错越错。',
    }
  }
  return null
}

export const usePersonsStore = defineStore('persons', () => {
  const items = ref([])
  const total = ref(0)
  const page = ref(1)
  const size = ref(24)
  const loading = ref(false)
  const error = ref('')

  /** 家庭组下拉（人物库筛选用；库里没有家庭组时是空数组，不报错） */
  const families = ref([])

  /** 单人档案（P-05 头部 + Tab1 年代档统计） */
  const current = ref(null)
  const currentLoading = ref(false)

  /** Tab1：按年代档分组的照片（BucketTimeline 的输入形状） */
  const timeline = ref({ groups: [], limitPerBucket: 12 })
  const timelineLoading = ref(false)

  /**
   * Tab1 时间轴**下方**的「去过的地方」区块（R5）。
   *
   * ⚠️ 形状里**必须留 `photoTotal` / `locatedPhotoTotal`**：
   *    实测绝大多数人这个区块是空的（全库照片带地点的关联只有个位数），
   *    所以界面上「没有数据」是常态。只给一个空数组的话，用户看到一片空白，
   *    分不清「功能没做」与「地点线索没覆盖到他的照片」——
   *    带上 N/M 才能写出「该人 9 张照片中，0 张有地点信息」。
   */
  const places = ref({ places: [], photoTotal: 0, locatedPhotoTotal: 0 })
  const placesLoading = ref(false)

  /** Tab2：人脸样本两段 + 真实条数 */
  const faces = ref({ items: [], counts: { confirmed: 0, disputed: 0, pending: 0, stranger: 0 } })
  const facesLoading = ref(false)
  const facesTruncated = ref(false)

  const filters = ref({
    keyword: '',
    category: '',
    familyGroupCode: '',
    delFlag: '0',
  })

  /** 「显示已停用」是独立开关，默认关 —— 已停用的人不该出现在默认名册里 */
  const includeDisabled = ref(false)

  const hasFilter = computed(
    () =>
      Boolean(filters.value.keyword) ||
      Boolean(filters.value.category) ||
      Boolean(filters.value.familyGroupCode) ||
      includeDisabled.value,
  )

  /** 空状态引导用：库里一个脸都没有 => 该让人先导入联系人了 */
  const libraryEmpty = computed(() => total.value === 0 && !hasFilter.value)

  function toQuery() {
    return {
      page: page.value,
      size: size.value,
      keyword: filters.value.keyword || undefined,
      category: filters.value.category || undefined,
      familyGroupCode: filters.value.familyGroupCode || undefined,
      // 勾了「显示已停用」才放开 delFlag，否则后端默认仍会带出停用的人
      delFlag: includeDisabled.value ? undefined : '0',
      orderBy: 'photoCount',
      desc: 1,
    }
  }

  async function fetchPersons() {
    loading.value = true
    error.value = ''
    try {
      const data = await listPersons(toQuery())
      items.value = data?.items ?? []
      total.value = data?.total ?? 0
      return data
    } catch (e) {
      error.value = e?.message || '取人物列表失败'
      items.value = []
      total.value = 0
      throw e
    } finally {
      loading.value = false
    }
  }

  /** 家庭组下拉。失败时静默降级成空列表 —— 筛选框空着不影响主流程 */
  async function fetchFamilies() {
    if (families.value.length) return families.value
    try {
      const data = await listFamilies({ size: 200 })
      families.value = data?.items ?? data?.families ?? []
    } catch (e) {
      families.value = []
    }
    return families.value
  }

  /**
   * 新建家庭组（「编辑资料 / 新建人物」里的那条入口）。
   *
   * ⚠️ 建完必须**自己塞进 families**，不能建完再 `fetchFamilies()`：
   *    上面那个函数在列表非空时直接返回缓存 —— 库里原本就有组的话，
   *    新建的这个永远进不来，下拉里看不到刚建的组，又表现成「按钮没反应」。
   *    塞进去还有个好处：人物库页面的「家庭组」筛选、其它表单的下拉
   *    都是同一个 store，建一次全局可见。
   *
   * 返回服务端生成的 familyCode（`FM_<名字>`，重名自动加序号），
   * 调用方拿它直接把这个人归入新组，省掉「建完再回下拉里找一遍」。
   */
  async function createFamily(familyName) {
    const name = String(familyName || '').trim()
    const created = await createFamilyApi({ familyName: name })
    const code = String(created?.familyCode || '')
    const label = String(created?.familyName || name)
    if (code) {
      // 与 GET /families 同序（按 familyName），否则新组会孤零零吊在列表末尾
      const rest = families.value.filter((one) => one.familyCode !== code)
      families.value = [...rest, { familyCode: code, familyName: label, memberCount: 0 }]
        .sort((a, b) => String(a.familyName || '').localeCompare(String(b.familyName || '')))
    }
    return { familyCode: code, familyName: label }
  }

  async function fetchPerson(personCode) {
    currentLoading.value = true
    try {
      current.value = await getPerson(personCode)
      return current.value
    } finally {
      currentLoading.value = false
    }
  }

  /** Tab1 时间轴。空年代档后端已过滤，这里只负责把数据形状交给 BucketTimeline */
  async function fetchTimeline(personCode, params = {}) {
    timelineLoading.value = true
    try {
      const data = await getPersonTimeline(personCode, params)
      timeline.value = data || { groups: [] }
      return timeline.value
    } finally {
      timelineLoading.value = false
    }
  }

  /** Tab2 人脸样本。**只读** —— 移除/改判走 review 接口，改完重取本函数 */
  async function fetchFaces(personCode, params = {}) {
    facesLoading.value = true
    try {
      const data = await listPersonFaces(personCode, { limit: 400, ...params })
      faces.value = data || { items: [], counts: {} }
      facesTruncated.value = Boolean(data?.truncated)
      return faces.value
    } finally {
      facesLoading.value = false
    }
  }

  /**
   * 设 / 清某个人的**默认头像**（DR-41）。
   *
   * @param {string} personCode
   * @param {string} faceCode 人脸编码；**空串 = 清空**（回退到代表脸）
   *
   * ⚠️ 成功之后必须**重取** `fetchPerson` + `fetchFaces`，不能只改本地一个 ref：
   *    库里改的是 `pb_person.avatarFaceCode`，而「哪一张是默认」这个高亮在
   *    Tab2 样本、详情头部头像、人物库卡片**三处**都从它推 —— 只动一处的话
   *    三处会各说各话（最典型的是：样本上标了「默认」，头像却没变）。
   * ⚠️ 失败**不吞异常**（页面负责 Toast）：静默失败会让用户以为设上了。
   * ⚠️ 不重取 `items`（人物库那一页）：`/people` 每次进入都会自己 reload，
   *    在这里多打一次全页请求只是浪费。
   */
  async function setAvatar(personCode, faceCode) {
    const code = String(personCode || '')
    if (!code) throw new Error('personCode 不能为空')
    await setPersonAvatar(code, faceCode)
    await Promise.all([fetchPerson(code), fetchFaces(code)])
  }

  /**
   * Tab1 时间轴下方的「去过的地方」（`GET /persons/{code}/places`）。
   *
   * ⚠️ 失败时**不能把 places 清成空**：接口挂掉与「他确实没去过任何地点」
   *    在界面上长得一样（区块隐藏 + 一句 N/M 提示），而后者是正常状态。
   *    所以这里把 error 留给调用方（`request.js` 已经弹过 Toast），
   *    自己只保证 loading 收尾、并且**不覆盖上一次的成功结果**。
   */
  async function fetchPlaces(personCode, params = {}) {
    placesLoading.value = true
    try {
      const data = await getPersonPlaces(personCode, params)
      places.value = data || { places: [], photoTotal: 0, locatedPhotoTotal: 0 }
      return places.value
    } finally {
      placesLoading.value = false
    }
  }

  /** 一次把 P-05 需要的都取回来（头部 + 两个 Tab + 地点区块），避免页面自己编排 4 个请求 */
  async function fetchPersonDetail(personCode) {
    const head = await fetchPerson(personCode)
    await Promise.all([fetchTimeline(personCode), fetchFaces(personCode),
                       fetchPlaces(personCode)])
    return head
  }

  /**
   * 复位筛选。
   *
   * ⚠️ 必须**原地改**（Object.assign）而不是 `filters.value = {...}` 换一个新的：
   *    页面里的 `const filters = persons.filters` 拿到的是**当时那个对象**，
   *    换新对象之后页面手上的还是旧的 —— 「清除筛选」点了之后输入框里的字
   *    一个字都不会消失，watch 也看不到变化（它盯的也是旧对象）。
   */
  function resetFilters() {
    Object.assign(filters.value, {
      keyword: '',
      category: '',
      familyGroupCode: '',
      delFlag: '0',
    })
    includeDisabled.value = false
    page.value = 1
  }

  function clearPerson() {
    current.value = null
    timeline.value = { groups: [], limitPerBucket: 12 }
    faces.value = { items: [], counts: { confirmed: 0, disputed: 0, pending: 0, stranger: 0 } }
    facesTruncated.value = false
    // ⚠️ 地点区块也要复位：不清的话，从 A 的详情跳到 B（或返回列表再进来）
    //    会先渲染出**上一个人的地点**，而它看起来完全合理 —— 只是错了。
    places.value = { places: [], photoTotal: 0, locatedPhotoTotal: 0 }
  }

  return {
    items,
    total,
    page,
    size,
    loading,
    error,
    families,
    current,
    currentLoading,
    timeline,
    timelineLoading,
    faces,
    facesLoading,
    facesTruncated,
    places,
    placesLoading,
    filters,
    includeDisabled,
    hasFilter,
    libraryEmpty,
    toQuery,
    fetchPersons,
    fetchFamilies,
    createFamily,
    fetchPerson,
    fetchPersonDetail,
    fetchTimeline,
    fetchFaces,
    setAvatar,
    fetchPlaces,
    resetFilters,
    clearPerson,
  }
})