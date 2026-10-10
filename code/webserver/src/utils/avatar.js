/* ============================================================
 * 人物头像的**读取口径**（DR-40）
 * ============================================================
 * 「这个人的头像长什么样」在服务端只有一个出口：`personCoversOf()`
 * （用户指定的默认 → 代表脸，人工确认优先）。它把结果放在 `coverFaceCode`
 * 里，并顺手拼好 `thumbUrl`。
 *
 * ⚠️ 为什么不能只读 `avatarFaceCode`
 * --------------------------------
 *   `avatarFaceCode` 是「用户**手工指定**的默认」，正式库里绝大多数为空
 *   （唯一写入口是人物详情 Tab2 的「设为默认」）。只读它的后果是：
 *   一整列人里只有少数几个有头像，其余是一个**空的灰圆** ——
 *   不报错、不是破图，只有用眼睛看才会发现。
 *   这个坑已经踩过三次（人物卡片 → 照片详情「出现的人」→ 改判浮层的搜索结果），
 *   所以把「该读哪个字段」钉在这一处。
 *
 * ⚠️ 为什么还要留 `avatarFaceCode` 兜底
 * -----------------------------------
 *   待确认队列的候选走的是**另一条数据链**：`reviewQueue.personCards()` 里
 *   名字就叫 `avatarFaceCode`（它自己按「人工确认样本里 detScore 最高」兜过底，
 *   刻意不用自动样本）。两套形状在前端的列表里会混在一起（改判浮层的
 *   搜索结果 + 相似度候选），所以取 `coverFaceCode || avatarFaceCode`。
 *
 * 三级回退（顺序是产品口径，别改）
 * ------------------------------
 *   ① **人脸裁剪图**（`coverFaceCode` / `avatarFaceCode`）—— 人物库 / 照片
 *      详情 / 待确认队列都用它，因为「人脸裁剪图」比照片缩略图更容易认人；
 *   ② **通讯录头像**（`contactAvatarUrl`，vCard 里内嵌的那张 PHOTO）——
 *      **通讯录导入的人往往一张脸都没有**，没有这一级他们永远只有首字母
 *      （正式库 1012 人已落盘却一直没入口）；
 *   ③ **姓名首字母**。
 *
 * ⚠️ 前两级都可能 404（裁剪图/头像文件还没落盘，服务端刻意不为列表探测
 *    文件系统）⇒ 组件**必须**接 `onError` 往后一级退，否则用户看到的是
 *    一个破图图标（比没有头像更难解释）。`useAvatarSources()` 就是这条链。
 */
import { computed, ref, unref, watch } from 'vue'
import { faceUrl } from '@/api/static'


/**
 * 该展示的那张脸（空串 = 这个人一张脸都没有，退首字母）。
 *
 * @param {{coverFaceCode?: string, avatarFaceCode?: string}} person
 * @returns {string}
 */
export function avatarFaceCodeOf(person) {
  return String(person?.coverFaceCode || person?.avatarFaceCode || '')
}

/**
 * 首字母占位：中文取第一个字，英文取首字母（与 PersonCard 同口径）。
 * 没有姓名时退回 personCode 的第一个字符 —— 空圆里什么都读不出来。
 *
 * @param {{displayName?: string, personCode?: string}} person
 * @returns {string}
 */
export function initialOf(person) {
  const name = String(person?.displayName || person?.personCode || '?').trim()
  return name.charAt(0) || '?'
}

/**
 * 这个人**依次尝试**的头像 URL（可能为空数组 = 直接首字母）。
 *
 * 顺序即产品口径：人脸裁剪图 → 通讯录头像。服务端给的是完整 URL
 * （`contactAvatarUrl`），前端的请求前缀只有 `api/static.js` 一处知道。
 *
 * @param {{coverFaceCode?: string, avatarFaceCode?: string, contactAvatarUrl?: string}} person
 * @returns {string[]}
 */
export function avatarSourcesOf(person) {
  const out = []
  const faceCode = avatarFaceCodeOf(person)
  if (faceCode) out.push(faceUrl(faceCode))
  const contact = String(person?.contactAvatarUrl || '')
  if (contact) out.push(contact)
  return out
}

/**
 * 取出「当前这个 person」：**可能是 getter、可能是 ref、也可能是对象**。
 *
 * ⚠️⚠️ 这里踩过一次坑，写清楚免得再踩：
 *   `unref()` **不会调用函数** —— 它只拆 ref。调用方习惯写
 *   `useAvatarSources(() => props.person)`（getter 是必须的：传值的话 props
 *   变了 computed 也不会重算），如果这里直接 `unref(person)`，拿到的是
 *   **那个函数对象**：`fn.displayName` / `fn.coverFaceCode` 全是 undefined
 *   ⇒ 既没有图（sources 为空）又没有名字（首字母退化成 `?`）。
 *   症状：**人物库、改判浮层、照片详情整片变成「?」**，而且不报错。
 */
function resolvePerson(person) {
  if (typeof person === 'function') return person()
  return unref(person)
}

/**
 * 头像的「逐级降级」状态机（`<script setup>` 里直接解构用）。
 *
 * 用法（**单个**人物的地方，如人物卡片 / 详情头部 / 单行组件）：
 *   const { src, showImage, onError, initial } = useAvatarSources(() => props.person)
 *   <img v-if="showImage" :src="src" @error="onError" />
 *   <span v-else>{{ initial }}</span>
 *
 * ⚠️ 为什么要有它：`@error` 之后该退到哪一级、什么情况下才显示首字母，
 *    在每个组件里各写一遍必然出现「这个页面会退、那个页面挂破图」。
 *
 * ⚠️ `person` 传 **getter/ref**（`() => props.person`），不是值：
 *    传值的话 props 变化时 computed 不会重算（拿到的是第一次那个对象）。
 *
 * @param {import('vue').Ref|Function|Object} person
 */
export function useAvatarSources(person) {
  const sources = computed(() => avatarSourcesOf(resolvePerson(person)))
  /** 当前尝试到第几级：0 = 人脸图，1 = 通讯录头像，>= length = 首字母 */
  const index = ref(0)
  // 换人 / 换头像文件（服务端给了新 URL）就从头再试：
  // 否则一次加载失败会把这一格**永久锁死**在首字母上。
  watch(sources, () => {
    index.value = 0
  })
  const src = computed(() => sources.value[index.value] || '')
  const showImage = computed(() => Boolean(src.value))
  function onError() {
    if (index.value < sources.value.length) index.value += 1
  }
  return {
    src,
    showImage,
    onError,
    initial: computed(() => initialOf(resolvePerson(person))),
    /** 还剩几级可退（排障用；0 = 再看不出图就只能首字母了） */
    remaining: computed(() => Math.max(0, sources.value.length - index.value)),
  }
}
