<!--
  AvatarPicker —— 「选择默认头像」弹窗（DR-41）
  ------------------------------------------------------------
  为什么独立成组件：`PersonDetailView.vue` 已经 800+ 行，而「人脸样本」
  那两段承担的是**四态语义 + 确认/移除动作**，选择器只承担**选择**。
  塞在一起会让两种取舍纠缠：样本段的每个改动都要担心别把选择器弄坏。

  三条纪律
  --------
  ① 描边语义**沿用 `faceStateOf`**（实线 = 人工确认 / 虚线 = 机器认的），
     不另写一套配色 —— 用户在 Tab2 认得的描边，在这里必须还是那个意思。
  ② 「当前默认」用**三重编码**：品牌色环 + ★ 图标 + 「默认」文字。
     DR-16② 的可访问性约束：颜色只能是**第四条**信息，灰度下也要能读。
  ③ **机器认的（虚线）样本也可以选**：头像只影响展示，不进质心、
     不改 `pb_face`、不参与匹配 —— 没必要逼用户先去确认那张脸。
-->
<script setup>
import { computed } from 'vue'
import { Star } from 'lucide-vue-next'
import { faceStateOf, faceStrokeStyle, similarityText } from '@/utils/faceState'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  /** 人物详情对象（要它的 displayName 与当前 avatarFaceCode） */
  person: { type: Object, default: null },
  /** 该人的全部人脸样本（`persons.faces.items`，含四态） */
  samples: { type: Array, default: () => [] },
  /** 样本被 limit 截断过（有 400 张上限，见 listPersonFaces） */
  truncated: { type: Boolean, default: false },
  submitting: { type: Boolean, default: false },
})

const emit = defineEmits(['update:modelValue', 'select', 'clear'])

/** 当前默认头像的 faceCode（空 = 没设过，展示的是服务端回退的代表脸） */
const currentFaceCode = computed(() => String(props.person?.avatarFaceCode || ''))

function isCurrent(sample) {
  return Boolean(sample?.faceCode) && String(sample.faceCode) === currentFaceCode.value
}

function metaOf(sample) {
  return faceStateOf(sample)
}

function ariaOf(sample) {
  const year = sample?.shotYear ? `${sample.shotYear} 年` : '未知年份'
  return `${year}的人脸样本，${metaOf(sample).label}${isCurrent(sample) ? '，当前默认头像' : ''}`
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    title="选择默认头像"
    width="720px"
    :close-on-click-modal="false"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div class="space-y-3">
      <p class="text-caption text-ink-sub">
        点一张脸即可设为「<b class="text-ink">{{ person?.displayName || '这个人' }}</b
        >」的默认头像。默认头像只影响<b>展示</b>（人物库卡片 + 详情头部），
        不进质心、不影响识别 —— 所以<b>机器认的（虚线）也能选</b>。
      </p>

      <ul v-if="samples.length" class="flex max-h-[52vh] flex-wrap gap-3 overflow-y-auto">
        <li v-for="sample in samples" :key="sample.faceCode" class="w-16">
          <button
            type="button"
            class="block w-full rounded-full focus:outline-none focus-visible:ring-2 focus-visible:ring-brand focus-visible:ring-offset-2"
            :aria-label="ariaOf(sample)"
            :aria-pressed="isCurrent(sample)"
            :disabled="submitting"
            @click="emit('select', sample.faceCode)"
          >
            <img
              :src="sample.thumbUrl"
              :alt="''"
              class="h-16 w-16 rounded-full object-cover"
              :class="[
                metaOf(sample).borderClass,
                isCurrent(sample) ? 'ring-2 ring-brand' : '',
                submitting ? 'opacity-60' : '',
              ]"
              :style="faceStrokeStyle(metaOf(sample))"
              loading="lazy"
            />
            <span class="mt-1 block text-center text-caption tabular-nums text-ink-weak">
              {{ similarityText(sample.similarity) }}
            </span>
          </button>
          <!-- 「当前默认」：图标 + 文字（不只靠颜色） -->
          <p
            v-if="isCurrent(sample)"
            class="mt-0.5 flex items-center justify-center gap-0.5 text-caption text-brand-ink"
          >
            <Star class="h-3 w-3" aria-hidden="true" />默认
          </p>
        </li>
      </ul>

      <p v-else class="rounded-btn bg-warning-soft px-3 py-2 text-caption text-warning-ink">
        这个人还没有任何人脸样本 —— 先在待确认队列里认几张脸，这里才有可选的图。
      </p>

      <p v-if="samples.length" class="pb-hint">
        实线 = 你确认过的 ｜ 虚线 = 机器认的（还没确认）。
        <template v-if="truncated">样本较多，这里只列了前 400 张。</template>
      </p>
    </div>

    <template #footer>
      <div class="flex items-center justify-between gap-3">
        <p class="pb-hint">
          {{
            currentFaceCode
              ? '清除后卡片会回到「自动代表脸」'
              : '还没设过默认，卡片用的是自动代表脸'
          }}
        </p>
        <div class="flex gap-2">
          <el-button
            :disabled="!currentFaceCode"
            :loading="submitting"
            @click="emit('clear')"
          >
            清除默认
          </el-button>
          <el-button @click="emit('update:modelValue', false)">关闭</el-button>
        </div>
      </div>
    </template>
  </el-dialog>
</template>
