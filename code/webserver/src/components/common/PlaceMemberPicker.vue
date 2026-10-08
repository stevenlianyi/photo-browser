<!--
  PlaceMemberPicker —— 地点详情的「同组下钻」分段选择器（R5 · DR-36）
  ------------------------------------------------------------
  归并项（中文名相同的多个 placeCode）点进来之后，用户要能回答
  「这 15 张里哪 8 张来自大屯、哪 7 张来自望京」。这一块就是那个开关。

  ⚠️ 拆成组件的理由（不是"为了短"）：**标签怎么取**是一条容易做错的规则，
    内联在页面里会跟着布局改动被顺手改掉：
      · 直接用 `placeDisplayName()` -> 组内每个点都显示「北京市 · 朝阳区」，
        两个按钮一模一样，用户根本不知道自己点的是哪一个（下钻等于没有）；
      · 真正区分它们的是**英文聚合键**（`CN, Beijing, Datun` /
        `CN, Beijing, Wangjing`），这里取它的**末段**（Datun / Wangjing）。
      · 目录名地点没有英文键（`placeName` 本身就是中文）-> 回退显示名。
  纯展示 + 一个 `update:modelValue`，不碰任何请求（页面负责重取子资源）。
-->
<script setup>
import { computed } from 'vue'
import { placeDisplayName, placeRawName } from '@/api/place'

const props = defineProps({
  /** `GET /places/{code}` 的 members[]（每个来源点一条） */
  members: { type: Array, default: () => [] },
  /** 归并后的总张数（「全部」那一段用） */
  total: { type: Number, default: 0 },
  /** '' = 全部；否则是组内某一个 placeCode */
  modelValue: { type: String, default: '' },
})

const emit = defineEmits(['update:modelValue'])

function memberLabel(member) {
  const raw = placeRawName(member)
  if (!raw) return placeDisplayName(member)
  const last = raw.split(',').pop().trim()
  return last || raw
}

/** 只有一个来源点时不出这个控件（没什么可下钻的，摆着只会占地方） */
const visible = computed(() => (props.members || []).length > 1)

const segments = computed(() => {
  const list = [{ code: '', label: '全部', count: Number(props.total) || 0 }]
  for (const one of props.members || []) {
    list.push({ code: String(one.placeCode || ''), label: memberLabel(one),
                count: Number(one.photoCount) || 0 })
  }
  return list
})
</script>

<template>
  <div v-if="visible" class="pb-card p-2">
    <el-radio-group
      :model-value="modelValue"
      size="small"
      aria-label="同组地点下钻"
      @update:model-value="emit('update:modelValue', $event)"
    >
      <el-radio-button
        v-for="seg in segments"
        :key="seg.code || 'all'"
        :value="seg.code"
      >
        {{ seg.label }}（{{ seg.count }} 张）
      </el-radio-button>
    </el-radio-group>
    <p class="pb-hint mt-2">
      同一中文名下的多个来源点分开列出（这是<b>展示层归并</b>，`placeCode` 一个都没改）。
    </p>
  </div>
</template>
