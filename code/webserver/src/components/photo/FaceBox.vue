<!--
  FaceBox —— 人脸框叠加（设计稿 §五 + §4.4 + DR-16）
  ------------------------------------------------------------
  **全项目纠错闭环的入口都在这个组件里**。三条设计红线：

  ① **人脸框永远可见**（P0-1）
     四态全部渲染框：人工确认 / 机器自动 / 待确认 / 陌生人。
     ⚠️ 设计稿状态表里「陌生人」写的是"描边：无"，但那会让框**在画面上不存在**，
     用户会以为检测漏了脸 —— 与 P0-1 直接冲突。折中：中性灰 + 1px 实线，
     颜色/描边/图标/文字四维都与另三态可区分，同时不与「可操作」的正色抢注意力。

  ② **四重编码**（色 + 描边 + 图标 + 文字）
     灰度打印、色盲、单色屏下都必须能分辨。颜色只能是第四条信息。
     描边样式额外承担**归属来源**这一维：
       实线 = 人工确认（isConfirmed=1）
       虚线 = 机器自动认的（isConfirmed=0，**可否决**）
       点线 = 待确认

  ③ **「✗ 不是他」一次点击可达**（P0-6）
     悬停即出，不必绕到人物详情 —— 用户在照片流里看到认错的第一反应就是就地纠正。
     键盘用户等价：Tab 聚焦到框内按钮即可（`focus-within` 同样触发显形）。
     图标 + 文字双编码，不只靠颜色。

  悬停提示里必须写清**是谁被认的**和**机器有多确信**：
  「机器认的，可否决」这行字是用户判断「值不值得点进去」的依据，
  只给一个绿框等于让用户凭感觉点。
-->
<script setup>
import { computed } from 'vue'
import { X } from 'lucide-vue-next'
import { faceBoxStyle, faceStateOf, faceStrokeStyle, similarityText } from '@/utils/faceState'

const props = defineProps({
  /** pb_face 派生的一行（getPhoto 的 faces[] 元素） */
  face: { type: Object, required: true },
  /** 归属人姓名（由外层解析：getPhoto 的 persons[] 与 faces[] 是分开的两份数据） */
  displayName: { type: String, default: '' },
  /** 相似度（没有可比质心时为 null —— 必须显示「—」而不是 0.00） */
  similarity: { type: Number, default: null },
  /** 悬停是否出「✗ 不是他」（关掉 = 纯展示模式，如只读预览） */
  actionable: { type: Boolean, default: true },
  /** 标签位置：框上方空间不够时翻到下方 */
  labelBelow: { type: Boolean, default: false },
})

const emit = defineEmits(['fix'])

const meta = computed(() => faceStateOf(props.face))
const boxStyle = computed(() => faceBoxStyle(props.face?.bbox))
const stroke = computed(() => faceStrokeStyle(meta.value))

/** 归属人：没归属就明说「未归属」，不要显示空白让人以为是渲染失败 */
const whoText = computed(() => {
  const name = String(props.displayName || '').trim()
  if (meta.value.key === 'pending') return '未归属'
  if (meta.value.key === 'stranger') return '陌生人'
  return name || `（${props.face?.personCode || '未知'}）`
})

/** 悬停提示：人名 + 相似度 + 状态文字 + 图标（四重编码全在这里） */
const hintText = computed(() => {
  const parts = [meta.value.icon, whoText.value]
  if (props.similarity !== null && props.similarity !== undefined) {
    parts.push(`相似度 ${similarityText(props.similarity)}`)
  }
  parts.push(meta.value.hint)
  return parts.join(' · ')
})

/** 陌生人没有「否决」可点 —— 它已经是最终态了 */
const canFix = computed(() => props.actionable && meta.value.key !== 'stranger')

const ariaLabel = computed(
  () => `人脸框：${hintText.value}${canFix.value ? '，激活后可改判' : ''}`,
)
</script>

<template>
  <div
    v-if="boxStyle"
    class="group/face absolute"
    :style="boxStyle"
    role="group"
    :aria-label="ariaLabel"
  >
    <!-- 框体：2px 描边 + 悬停加粗（键盘 focus-within 同样触发） -->
    <div
      class="absolute inset-0 rounded-[2px] transition-[box-shadow,border-width] duration-150"
      :class="[meta.borderClass, canFix ? 'group-hover/face:border-2' : '']"
      :style="stroke"
    />

    <!-- 标签：常驻小徽标（四重编码常驻，不需要 hover 才看得到） -->
    <span
      class="pointer-events-none absolute left-0 z-10 inline-flex max-w-[220px] items-center gap-1 whitespace-nowrap rounded-btn px-1.5 py-0.5 text-caption shadow-pop"
      :class="[meta.softClass, meta.textClass, labelBelow ? 'top-full mt-1' : 'bottom-full mb-1']"
    >
      <span aria-hidden="true">{{ meta.icon }}</span>
      <span class="truncate">{{ whoText }}</span>
      <span
        v-if="similarity !== null && similarity !== undefined"
        class="tabular-nums opacity-80"
      >
        {{ similarityText(similarity) }}
      </span>
      <span class="sr-only">{{ meta.hint }}</span>
    </span>

    <!--
      纠错入口：悬停 / 键盘聚焦即出，**一次点击**打开改判浮层。
      按钮是框的 DOM 子元素，所以鼠标从框移到按钮时 :hover 不会断
      （父元素的 :hover 在指针位于任意后代时都成立）。
    -->
    <button
      v-if="canFix"
      type="button"
      class="absolute left-0 z-20 inline-flex items-center gap-1 whitespace-nowrap rounded-btn border border-line bg-card px-1.5 py-0.5 text-caption text-danger-ink opacity-0 shadow-pop transition-opacity duration-150 hover:bg-danger-soft focus-visible:opacity-100 group-hover/face:opacity-100"
      :class="labelBelow ? 'bottom-full mb-1' : 'top-full mt-1'"
      :aria-label="`这不是${whoText}，改判这张脸`"
      :title="`这不是${whoText}？点一下改判`"
      @click.stop.prevent="emit('fix', face)"
    >
      <X class="h-3 w-3" aria-hidden="true" />
      不是他
    </button>
  </div>
</template>
