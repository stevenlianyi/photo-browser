<!--
  CandidateRow —— 候选人物行（设计稿 §五「关键组件」表）
  ------------------------------------------------------------
  头像 + 姓名 + 相似度 + 确认按钮，按相似度**降序**（顺序由后端
  /api/review/pending 的 topCandidates 保证，前端不再排一次 —— 两处排序
  口径不一致时，用户看到的"第一名"会不是他按的那个键）。

  三处刻意的设计
  --------------
  ① **序号常驻在左**（`1` / `2` / `3`）。它不是装饰，是键盘快捷键的
     **可见对应物**：用户按「2」之前得先知道第二个是谁。序号 + 快捷键提示
     写在一起，肌肉记忆才建得起来。
  ② **灰区条目橙色提示**。相似度落在 T_low–T_high 的候选**不做自动归属**
     （P0-2「不确定就要问」），所以这几条本身就是"机器没把握"的那些 ——
     橙色提示不是警告，是**在解释为什么这里要你动手**。
  ③ **关系提示来自 `relation` 字段**（如「兄妹，长相接近」）。
     这不是算出来的相似度能表达的信息：0.58 和 0.62 谁更可能是同一个人，
     只有知道"这俩是兄妹"才判断得了。所以它必须显示，且**用橙色**（警示）
     而不是灰色（备注）。
-->
<script setup>
import { computed } from 'vue'
import { TriangleAlert, X } from 'lucide-vue-next'
import { faceUrl } from '@/api/static'
import { similarityText } from '@/utils/faceState'

const props = defineProps({
  /** { personCode, displayName, avatarFaceCode, similarity, relation, bucketKey } */
  candidate: { type: Object, required: true },
  /** 左上角序号（0 起）；对应键盘 1/2/3 */
  index: { type: Number, default: 0 },
  thresholdLow: { type: Number, default: 0.3 },
  thresholdHigh: { type: Number, default: 0.45 },
  loading: { type: Boolean, default: false },
  /** 高亮：键盘按下时会选中这一条 */
  active: { type: Boolean, default: false },
})

const emit = defineEmits(['confirm', 'fix'])

const ordinal = computed(() => props.index + 1)

/** 灰区 = 机器没把握的区间，正是需要人工判断的那些 */
const inGreyZone = computed(() => {
  const value = props.candidate?.similarity
  if (value === null || value === undefined) return false
  const num = Number(value)
  return num >= props.thresholdLow && num <= props.thresholdHigh
})

const hasAvatar = computed(() => Boolean(props.candidate?.avatarFaceCode))
</script>

<template>
  <li
    class="flex items-center gap-3 px-3 py-2 transition-colors duration-150"
    :class="active ? 'bg-brand-soft' : 'hover:bg-surface'"
  >
    <!-- 序号 = 键盘快捷键（1/2/3）的可见对应物 -->
    <kbd
      class="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-btn border border-line bg-card text-caption font-medium tabular-nums text-ink-sub"
      :title="`按 ${ordinal} 确认`"
      >{{ ordinal }}</kbd
    >

    <img
      v-if="hasAvatar"
      :src="faceUrl(candidate.avatarFaceCode)"
      class="h-10 w-10 shrink-0 rounded-full border border-line object-cover"
      :alt="`${candidate.displayName} 的头像`"
    />
    <span
      v-else
      class="h-10 w-10 shrink-0 rounded-full border border-line bg-surface"
      aria-hidden="true"
    />

    <span class="min-w-0 flex-1">
      <span class="block truncate text-body text-ink">{{ candidate.displayName }}</span>
      <!-- 关系提示：来自 pb_person.relation，机器相似度表达不了的信息 -->
      <span
        v-if="candidate.relation"
        class="mt-0.5 flex items-center gap-1 text-caption text-warning-ink"
      >
        <TriangleAlert class="h-3 w-3 shrink-0" aria-hidden="true" />
        <span class="truncate">{{ candidate.relation }}</span>
      </span>
    </span>

    <!-- 相似度：灰区用橙色（文字 + 「灰区」标签双编码，不只靠颜色） -->
    <span class="shrink-0 text-right">
      <span
        class="block text-body tabular-nums"
        :class="inGreyZone ? 'text-warning-ink' : 'text-ink-sub'"
      >
        {{ similarityText(candidate.similarity) }}
      </span>
      <span
        v-if="inGreyZone"
        class="block text-caption text-warning-ink"
        title="相似度落在灰区：机器没把握，需要人工判断"
        >灰区</span
      >
    </span>

    <el-button
      size="small"
      type="primary"
      :loading="loading"
      :aria-label="`确认这张脸属于 ${candidate.displayName}`"
      @click="emit('confirm', candidate)"
    >
      确认
    </el-button>
    <el-button
      size="small"
      text
      type="danger"
      :aria-label="`这张脸也不是 ${candidate.displayName}，换一个`"
      @click="emit('fix', candidate)"
    >
      <X class="mr-1 h-3.5 w-3.5" aria-hidden="true" />改判
    </el-button>
  </li>
</template>
