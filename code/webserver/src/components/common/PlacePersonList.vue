<!--
  PlacePersonList —— 地点详情 Tab2「在场的人」（R5）
  ------------------------------------------------------------
  从 `PlaceDetailView.vue` 拆出来的（那个文件拆之前 342 行，超过「单文件 ≤300 行」
  的硬约束）。拆的是**一整块有独立语义的东西**：一个「地点里有哪些人」的列表，
  三态（加载中 / 空 / 有数据）都由它自己负责 ——

  ⚠️ 为什么空状态必须自己带引导文案（而不是留给页面）
  --------------------------------------------------
  实测：全库 663 张「有地点」的照片里，已关联到人物的只有个位数
  —— 所以**这个列表绝大多数时候是空的**，空白是它的常态而不是异常。
  只写「暂无」会让人分不清「功能没做」与「地点线索没覆盖到」；
  这里给出**下一步该去哪**（待确认队列）+ 这处一共有多少张照片，
  用户才知道这不是他点错了地方。

  ⚠️ 人名一律走 `person.displayName`（接口已从 `pb_person` 取好），
     `personCode` 只作为链接与兜底文案 —— `CS_0154_Qing_Bai` 这种编码
     不该出现在界面上。
-->
<script setup>
import { RouterLink } from 'vue-router'
import { UserRound } from 'lucide-vue-next'
import { faceUrl } from '@/api/static'
import { EMPTY, formatCount } from '@/utils/format'

const props = defineProps({
  /** `GET /places/{code}/persons` 的 items[] */
  items: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
  /** 该地点的照片总数（空状态文案里要带上，见文件头） */
  photoTotal: { type: Number, default: 0 },
})

/** 年份跨度：只有一年时不要写成 `2013–2013`（那看起来像渲染坏了） */
function yearSpanOf(person) {
  const low = person?.firstShotYear
  const high = person?.lastShotYear
  if (!low && !high) return EMPTY
  if (!low || !high || String(low) === String(high)) return String(low || high)
  return `${low}–${high}`
}
</script>

<template>
  <div class="space-y-3 pt-2">
    <p v-if="loading" class="pb-hint">正在读取在场的人…</p>

    <p v-else-if="!items.length" class="pb-hint">
      这个地点（共 {{ formatCount(props.photoTotal) }} 张照片）还没有关联到人物。
      在
      <RouterLink to="/review" class="text-brand-ink">待确认队列</RouterLink>
      里给这处的照片确认人脸之后，这里会显示当时在场的人。
    </p>

    <ul v-else class="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-4">
      <li v-for="person in items" :key="person.personCode">
        <RouterLink
          :to="`/people/${person.personCode}`"
          class="pb-card flex items-center gap-3 p-3 transition-transform duration-150 ease-out hover:-translate-y-0.5"
          :aria-label="`${person.displayName}：在这个地点出现 ${person.photoCount} 张照片，`
            + yearSpanOf(person)"
        >
          <span
            class="flex h-10 w-10 shrink-0 items-center justify-center overflow-hidden rounded-full border border-line bg-surface"
            aria-hidden="true"
          >
            <img v-if="person.thumbUrl" :src="faceUrl(person.avatarFaceCode)"
                 alt="" class="h-full w-full object-cover" loading="lazy" />
            <UserRound v-else class="h-5 w-5 text-ink-weak" />
          </span>
          <span class="min-w-0">
            <span class="block truncate text-body text-ink">
              {{ person.displayName || person.personCode }}
            </span>
            <span class="block text-caption tabular-nums text-ink-weak">
              {{ formatCount(person.photoCount) }} 张 · {{ yearSpanOf(person) }}
            </span>
          </span>
        </RouterLink>
      </li>
    </ul>
  </div>
</template>
