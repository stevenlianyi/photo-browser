<!--
  PersonCard —— 人物库的一格（设计稿 §4.8 顶栏下的卡片网格）
  ------------------------------------------------------------
  本步**唯一新建的组件**。为什么要独立成组件而不是内联在 PeopleView：
  ① 卡片的信息密度与「识别质量徽标」的判定规则是一份知识，
     内联的话它会跟着页面布局改动被顺手改掉；
  ② 头像那一块（圆形遮罩 + /api/face 兜底 + 停用态）有 4~5 种状态，
     塞进页面模板会让 PeopleView 变成一坨。

  三个刻意的取舍
  --------------
  ① **头像永远是「人脸裁剪图」，不是照片缩略图**。
     人脸图是 160×160 正方形（步骤 4），圆形遮罩直接套；用缩略图当头像
     会拍到一张脸很小的合影里，用户根本认不出是谁。
     取哪一张脸**两级**（DR-40）：**用户指定的默认**（`avatarFaceCode`，
     唯一写入口是人物详情 Tab2 的「设为默认」，DR-41）→ 服务端算好的
     **代表脸**（`coverFaceCode`：人工确认优先 → detScore/quality 最高）。
     两级都空（这个人一张脸都没有）才退到首字母占位 —— **不退回灰色问号**：
     「还没认出来」和「这是个陌生人」在界面上必须长得不一样。
  ② **徽标只用色 + 图标 + 文字三重编码**（不是只靠颜色）：
     DR-16② 的核心可访问性约束，灰度/色盲下也要能读。
  ③ **hover 上浮而不是缩放**：缩放会让相邻卡片的可点区域在动画中错位，
     上浮只改 transform: translateY，不影响布局。
-->
<script setup>
import { computed, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { CircleSlash, TriangleAlert, UserRound } from 'lucide-vue-next'
import { faceUrl } from '@/api/static'
import { healthOf, relationLabelOf } from '@/store/persons'
import { EMPTY } from '@/utils/format'

const props = defineProps({
  /** personSummary 形状（/api/persons 的一行） */
  person: { type: Object, required: true },
})

const emit = defineEmits(['edit', 'disable'])

/** 停用的人默认不显示；显示时要有明确标记，否则用户会以为数据丢了 */
const isDisabled = computed(() => String(props.person?.delFlag) === '1')

/**
 * 头像脸码 = 服务端解析好的 `coverFaceCode`（DR-40）。
 *
 * ⚠️ **不要**再用 `avatarFaceCode` 兜底：它只是「用户选过的那张」，
 *    可能已经**失效**（那张脸被移除/合并走了，而 DR-41④ 刻意不在写路径上清理）。
 *    真正的两级回退（默认 → 代表脸）在服务端 `personCoversOf()` 里做完了，
 *    这里认一个字段就够 —— 两端各退一次会出现两套口径。
 */
const avatarFaceCode = computed(() => props.person?.coverFaceCode || '')

const avatarSrc = computed(() => (avatarFaceCode.value ? faceUrl(avatarFaceCode.value) : ''))

/**
 * ⚠️ 裁剪图**可能不在磁盘上**（`/api/face` 对查不到的图回 404，见
 *    api/static.py 的 getFace：「库里查得到但裁剪图还没落盘」也会 404）。
 *    那时必须回退首字母 —— 卡片上挂一个破图图标比没有头像更难解释。
 *    服务端刻意不做 exists 探测（列表接口不碰文件系统），所以这件事只能在
 *    这里兜。
 */
const avatarBroken = ref(false)
// 换了人 / 换了默认头像就重置：否则一次加载失败会把这个组件「永久锁死」在
// 首字母状态（列表刷新后明明有了新头像也不显示）。
watch(avatarSrc, () => {
  avatarBroken.value = false
})
const showAvatar = computed(() => Boolean(avatarSrc.value) && !avatarBroken.value)

/** 首字母：中文取第一个字，英文取首字母大写 */
const initial = computed(() => String(props.person?.displayName || '?').trim().charAt(0) || '?')

/**
 * 年代跨度：`1998–2021`。只有一年时不要写成 `2007–2007` ——
 * 那看起来像渲染坏了。
 */
const yearSpan = computed(() => {
  const low = props.person?.yearLow
  const high = props.person?.yearHigh
  if (!low && !high) return EMPTY
  if (!low || !high || String(low) === String(high)) return String(low || high)
  return `${low}–${high}`
})

const photoCount = computed(() => Number(props.person?.photoCount) || 0)
const faceCount = computed(() => Number(props.person?.faceCount) || 0)
const confirmedCount = computed(() => Number(props.person?.confirmedFaceCount) || 0)

const health = computed(() => healthOf(props.person))

const ariaLabel = computed(
  () =>
    `${props.person?.displayName || '未命名'}，${photoCount.value} 张照片，`
    + `${faceCount.value} 张人脸（其中 ${confirmedCount.value} 张已人工确认），`
    + `年代跨度 ${yearSpan.value}`
    + (isDisabled.value ? '，已停用' : '')
    + (health.value ? `，${health.value.label}` : ''),
)
</script>

<template>
  <article
    class="pb-card group relative flex flex-col p-4 transition-transform duration-150 ease-out hover:-translate-y-0.5 focus-within:-translate-y-0.5"
  >
    <!-- hover / 聚焦出 [编辑] [停用]。不提供「删除」（DR-19） -->
    <div
      class="absolute right-2 top-2 flex gap-1 opacity-0 transition-opacity duration-150 focus-within:opacity-100 group-hover:opacity-100"
    >
      <el-button size="small" text @click="emit('edit', person)">编辑</el-button>
      <el-button size="small" text type="danger" @click="emit('disable', person)">
        停用
      </el-button>
    </div>

    <RouterLink
      :to="`/people/${person.personCode}`"
      class="block focus:outline-none"
      :aria-label="ariaLabel"
    >
      <!-- 头像：圆形遮罩。人脸图已按 160×160 正方形输出，**铺满**圆框。
           ⚠️ 不要在圆框内留底色环「把照片缩小」：试过 `p-2` + 内层 `rounded-full`
           把脸缩到 ~75%，观感变成两个同心圈（双圈），2026-10-08 用户实拍后否决。 -->
      <span
        class="relative mx-auto block h-24 w-24 overflow-hidden rounded-full border-2 bg-surface"
        :class="isDisabled ? 'border-line opacity-60' : 'border-brand-soft'"
        aria-hidden="true"
      >
        <img
          v-if="showAvatar"
          :src="avatarSrc"
          :alt="''"
          class="h-full w-full object-cover"
          decoding="async"
          loading="lazy"
          draggable="false"
          @error="avatarBroken = true"
        />
        <span
          v-else
          class="flex h-full w-full items-center justify-center text-title text-ink-weak"
        >
          <UserRound v-if="!initial" class="h-7 w-7" />
          <template v-else>{{ initial }}</template>
        </span>
      </span>

      <span class="mt-3 block text-center">
        <span class="block truncate text-body text-ink" :title="person.displayName">
          {{ person.displayName || person.personCode }}
        </span>
        <span class="mt-0.5 block truncate text-caption text-ink-weak">
          <!-- 关系显示中文（库里存的是 parent/spouse/… 英文码） -->
          <template v-if="person.relation">{{ relationLabelOf(person.relation) }} · </template>
          {{ yearSpan }}
        </span>
      </span>

      <!-- 徽标区：照片数 + 识别质量 + 停用态 -->
      <span class="mt-3 flex flex-wrap items-center justify-center gap-1.5">
        <span class="rounded-btn bg-surface px-1.5 py-0.5 text-caption tabular-nums text-ink-sub">
          {{ photoCount }} 张
        </span>

        <span
          v-if="isDisabled"
          class="inline-flex items-center gap-1 rounded-btn bg-surface px-1.5 py-0.5 text-caption text-ink-weak"
          title="已停用：不参与匹配，人脸已退回待确认队列"
        >
          <CircleSlash class="h-3 w-3" aria-hidden="true" />已停用
        </span>

        <span
          v-else-if="health"
          class="inline-flex items-center gap-1 rounded-btn bg-warning-soft px-1.5 py-0.5 text-caption text-warning-ink"
          :title="health.hint"
        >
          <TriangleAlert class="h-3 w-3" aria-hidden="true" />{{ health.label }}
        </span>

        <span
          v-else-if="faceCount === 0"
          class="inline-flex items-center gap-1 rounded-btn bg-surface px-1.5 py-0.5 text-caption text-ink-weak"
          title="还没有任何脸归属给他 —— 多半是刚导入的联系人"
        >
          还没有脸
        </span>
      </span>
    </RouterLink>

    <!-- 徽标的完整说明常驻在下方一行：徽标只有 6 个字，说不清「为什么」 -->
    <p
      v-if="health && !isDisabled"
      class="mt-2 text-center text-caption text-warning-ink"
      :title="health.hint"
    >
      {{ health.hint }}
    </p>
  </article>
</template>