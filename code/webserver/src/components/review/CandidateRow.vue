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
  ② **相似度分档，而不是只给一个数字**。0.22 和 0.05 长得一样，
 *     但前者「多半不是」、后者「接近随机基线」，所以数值下面必须有一句
 *     人话档位：高可靠 / 待判断 / 大概不是 / 基本不是。四档边界跟着
 *     T_low–T_high 走 —— 落在灰区的候选**不做自动归属**，
 *     橙色的「待判断」不是警告，是**在解释为什么这里要你动手**。
     （P0-2「不确定就要问」）：这一档不会自动归属。
  ③ **关系提示来自 `relation` 字段**（如「兄妹，长相接近」）。
     这不是算出来的相似度能表达的信息：0.58 和 0.62 谁更可能是同一个人，
     只有知道"这俩是兄妹"才判断得了。所以它必须显示，且**用橙色**（警示）
     而不是灰色（备注）。
-->
<script setup>
import { computed } from 'vue'
import { TriangleAlert, X } from 'lucide-vue-next'
import PersonAvatar from '@/components/common/PersonAvatar.vue'
import { confidenceOf, similarityText } from '@/utils/faceState'
import { relationLabelOf } from '@/store/persons'

const props = defineProps({
  /** { personCode, displayName, avatarFaceCode, coverFaceCode, contactAvatarUrl, similarity, relation, bucketKey } */
  candidate: { type: Object, required: true },
  /** 左上角序号（0 起）；对应键盘 1/2/3 */
  index: { type: Number, default: 0 },
  thresholdLow: { type: Number, default: 0.42 },
  thresholdHigh: { type: Number, default: 0.62 },
  loading: { type: Boolean, default: false },
  /** 高亮：键盘按下时会选中这一条 */
  active: { type: Boolean, default: false },
})

const emit = defineEmits(['confirm', 'fix'])

const ordinal = computed(() => props.index + 1)

/**
 * 相似度分档（≥T_high 高可靠 / 灰区 待判断 / 偏低 大概不是 / 极低 基本不是）。
 * 边界跟着设置里的 T_low/T_high 走 —— 用户调了阈值，这里跟着变，
 * 不会界面按旧口径说「高可靠」而后台已经按新口径自动归属了。
 */
const level = computed(() =>
  confidenceOf(props.candidate?.similarity, {
    low: props.thresholdLow,
    high: props.thresholdHigh,
  }),
)

// 头像由 PersonAvatar 统一处理（三级回退：人脸图 → 通讯录头像 → 首字母）。
// 这里曾经自己读 `candidate.avatarFaceCode`（= 用户手工指定的默认，绝大多数为空）
// ⇒ 一列候选里只有少数几个有头像，其余是空圆，且不报错。
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

    <!-- 头像 = 人脸图 → 通讯录头像 → 首字母（三级回退，见 PersonAvatar.vue） -->
    <PersonAvatar :person="candidate" size="md" />

    <span class="min-w-0 flex-1">
      <span class="block truncate text-body text-ink">{{ candidate.displayName }}</span>
      <!-- 关系提示：来自 pb_person.relation，机器相似度表达不了的信息 -->
      <span
        v-if="candidate.relation"
        class="mt-0.5 flex items-center gap-1 text-caption text-warning-ink"
      >
        <TriangleAlert class="h-3 w-3 shrink-0" aria-hidden="true" />
        <span class="truncate">{{ relationLabelOf(candidate.relation) }}</span>
      </span>
    </span>

    <!-- 相似度：数值 + 分档标签（文字 + 颜色双编码，色盲/灰度下仍可分辨） -->
    <span class="shrink-0 text-right">
      <span
        class="block text-body tabular-nums"
        :class="level.textClass"
        :aria-label="`相似度 ${similarityText(candidate.similarity)}，${level.hint}`"
      >
        {{ similarityText(candidate.similarity) }}
      </span>
      <span class="block text-caption" :class="level.textClass" :title="level.hint">
        {{ level.label }}
      </span>
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
