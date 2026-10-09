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

   ④ **标签不压脸**（原设计缺陷，这里补上）
     ------------------------------------
     早先标签只有「上 / 下」两个位置（`labelBelow`），而 `labelBelow`
     **全项目没有任何调用方传过** —— 等于从来没有边界处理。标签固定贴框的上沿之外，
     而人脸框是**紧贴脸**的，「框上方」不是天空，就是这个人的额头/眼睛，
     或者是合影里前一个人的脸。截图里那块糊在下巴上的「⚠ 未归属」就是这么来的；
     而人脸再往上一点，它又跑出画面。
     现在的规则见下面 `labelSide` 的注释：**默认贴框的左/右侧**（框外），
     左右都没空间才退回上下，最后才放进框内。
  -->
  <script setup>
  import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
  import { X } from 'lucide-vue-next'
  import {
    faceBoxStyle,
    faceStateOf,
    faceStrokeStyle,
    parseBbox,
    similarityText,
  } from '@/utils/faceState'

  const props = defineProps({
    /** pb_face 派生的一行（getPhoto 的 faces[] 元素） */
    face: { type: Object, required: true },
    /** 归属人姓名（由外层解析：getPhoto 的 persons[] 与 faces[] 是分开的两份数据） */
    displayName: { type: String, default: '' },
    /** 相似度（没有可比质心时为 null —— 必须显示「—」而不是 0.00） */
    similarity: { type: Number, default: null },
    /**
     * 「这张脸是照片里第几张**未归属**的脸」（1 起；0 / 不传 = 不显示编号）。
     *
     * 由外层算好传进来，**组件自己绝不排序 / 计数** —— 侧栏那个「未归属」清单
     * 用的是同一个 Map，编号一旦在这里重算就会与清单错位（错位比没有编号更坏：
     * 用户会照着错号去改判**另一张**脸）。
     * 只有 pending 才传：已归属的框上已经有人名，再编一套号只是往脸上堆字。
     */
    ordinal: { type: Number, default: 0 },
    /** 悬停是否出「✗ 不是他」（关掉 = 纯展示模式，如只读预览） */
    actionable: { type: Boolean, default: true },
  })

  const emit = defineEmits(['fix'])

  const meta = computed(() => faceStateOf(props.face))
  const boxStyle = computed(() => faceBoxStyle(props.face?.bbox))
  const box = computed(() => parseBbox(props.face?.bbox))
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

// ============================================================
// 标签放在哪一侧
// ============================================================

/** 标签与框之间的空隙（px），对应 Tailwind 的 mb-1 / ml-1 */
const GAP = 4

/** 标签高度粗估：text-caption(12px) + py-0.5 上下各 2px + 描边 */
const LABEL_H = 22

/** 照片容器的显示尺寸（px）。0 = 还没量到 */
const rootRef = ref(null)
const frameW = ref(0)
const frameH = ref(0)
let frameObserver = null

onMounted(() => {
  // 要「选边」就得知道照片在屏幕上有多宽，而这个宽度是 **CSS 算出来的**
  // （`max-width: min(100%, 70vh * 宽高比)`），JS 侧拿不到 ——
  // displaySize 给的是**原图像素**，不是显示尺寸。所以直接量**定位祖先**：
  // root 是 absolute，它的 offsetParent 就是照片容器。
  // ⚠️ 这样做的好处是**不用改任何调用方**：谁把 FaceBox 放进 relative 容器，
  //   谁就是被量的那一层，天然对齐。
  const host = rootRef.value?.offsetParent
  if (!host || typeof ResizeObserver === 'undefined') return
  const measure = () => {
    frameW.value = host.clientWidth
    frameH.value = host.clientHeight
  }
  measure()
  frameObserver = new ResizeObserver(measure)
  frameObserver.observe(host)
})

onBeforeUnmount(() => {
  frameObserver?.disconnect()
  frameObserver = null
})

/**
 * 标签宽度粗估（px）。**故意低估**：估多了顶多多翻一次边（只是不理想），
 * 估少了标签会多伸出照片一点 —— 父容器不裁切，文字仍然可读，
 * 而被**裁掉**才是不可接受的（那就等于没有标签）。
 */
function estimateLabelWidth(text) {
  let units = 0
  for (const ch of String(text || '')) {
    units += /[\u2e80-\u9fff\uff00-\uffef]/.test(ch) ? 1 : 0.55
  }
  return 20 + units * 12 // 图标 + 左右内边距 / 每字
}

/**
 * 标签放哪一侧 —— 「不压脸」优先，其次「不出画」。
 *
 *   ① **左 / 右**（框外）：绝不会压到这张脸。右侧放得下就右，否则左。
 *   ② 两侧都不够（脸贴着照片左右边缘）才回到**上下**，取空间大的一边。
 *   ③ 连上下也不够（框几乎占满画面）才放进**框内**左上角 ——
 *      宁可盖住额头也不出画：标签跑出画面等于信息丢失。
 *   ⚠️ 合影里并排的几张脸，标签会盖到邻居的脸上。这是**刻意接受**的：
 *      为了不盖住自己而把标签塞回上下，只会让它盖住**自己的**脸；
 *      「这张脸是谁」比「旁边那个人被挡了一小块」重要得多。
 */
const labelSide = computed(() => {
  const b = box.value
  const fw = frameW.value
  const fh = frameH.value
  // 量不到容器（bbox 无效 / 环境没有 ResizeObserver）-> 退回旧行为
  if (!b || !fw || !fh) return 'top'

  const labelText = `${meta.value.icon}${props.ordinal ? `#${props.ordinal}` : ''}${whoText.value}`
  const need = estimateLabelWidth(labelText) + GAP
  const leftSpace = b.x * fw
  const rightSpace = fw - (b.x + b.w) * fw
  if (rightSpace >= need) return 'right'
  if (leftSpace >= need) return 'left'

  const needH = LABEL_H + GAP
  const topSpace = b.y * fh
  const bottomSpace = fh - (b.y + b.h) * fh
  if (bottomSpace >= needH) return 'bottom'
  if (topSpace >= needH) return 'top'
  return 'inside'
})

/** 五个位置对应的 class。`top-1/2 -translate-y-1/2` = 与框垂直居中 */
const SIDE_CLASS = {
  top: 'left-0 bottom-full mb-1',
  bottom: 'left-0 top-full mt-1',
  left: 'right-full mr-1 top-1/2 -translate-y-1/2',
  right: 'left-full ml-1 top-1/2 -translate-y-1/2',
  inside: 'left-0 top-0',
}

/** 「✗ 不是他」放标签的**对侧**：两个徽标挤在同一侧会叠在一起 */
const fixSide = computed(() => {
  if (labelSide.value === 'right') return 'left'
  if (labelSide.value === 'left') return 'right'
  if (labelSide.value === 'bottom') return 'top'
  return 'bottom'
})

const ariaLabel = computed(() => {
  // 编号也要读得出来：只画在标签上等于对视障用户没有编号
  const num = props.ordinal ? `第 ${props.ordinal} 张未归属人脸，` : ''
  return `人脸框：${num}${hintText.value}${canFix.value ? '，激活后可改判' : ''}`
})
</script>

<template>
  <div
    v-if="boxStyle"
    ref="rootRef"
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

    <!-- 标签：常驻小徽标（四重编码常驻，不需要 hover 才看得到）。
         位置由 labelSide 自动选（默认贴框的左侧/右侧，绝不压脸）。 -->
    <span
      class="pointer-events-none absolute z-10 inline-flex max-w-[220px] items-center gap-1 whitespace-nowrap rounded-btn px-1.5 py-0.5 text-caption shadow-pop"
      :class="[meta.softClass, meta.textClass, SIDE_CLASS[labelSide]]"
    >
      <!-- 未归属的框带编号，侧栏「未归属的人脸」清单用同一个号 -->
      <span v-if="ordinal" class="font-medium tabular-nums">#{{ ordinal }}</span>
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
      位置固定是标签的**对侧** —— 同侧会与标签叠在一起。
    -->
    <button
      v-if="canFix"
      type="button"
      class="absolute z-20 inline-flex items-center gap-1 whitespace-nowrap rounded-btn border border-line bg-card px-1.5 py-0.5 text-caption text-danger-ink opacity-0 shadow-pop transition-opacity duration-150 hover:bg-danger-soft focus-visible:opacity-100 group-hover/face:opacity-100"
      :class="SIDE_CLASS[fixSide]"
      :aria-label="`这不是${whoText}，改判这张脸`"
      :title="`这不是${whoText}？点一下改判`"
      @click.stop.prevent="emit('fix', face)"
    >
      <X class="h-3 w-3" aria-hidden="true" />
      不是他
    </button>
  </div>
</template>
