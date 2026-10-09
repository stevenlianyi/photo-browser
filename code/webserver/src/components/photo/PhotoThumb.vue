<!--
  PhotoThumb —— 照片流网格里的一格（设计稿 §五「关键组件」表）
  ------------------------------------------------------------
  红线：**这里只允许出现 /api/thumb，绝不加载原图**（设计稿 §1.3）。
  实现上不是靠"记得别写 originalUrl"，而是这个组件**根本不接收**原图 URL ——
  src 由 photoCode 推导，调用方想传原图也没有入口。
  （验收第 2 条：打开照片流，Network 面板只该看到 /api/thumb。）

  三个交互细节
  ------------
  ① **懒加载用 IntersectionObserver 而不是 loading="lazy"**
     `loading="lazy"` 已经在请求队列里**排队**了（只是不阻塞渲染），
     一屏 60 张 + 预加载缓冲仍会打出一大批请求；IO 自己控制 `src` 何时赋值，
     没进视口的格子**一个字节都不请求**。10 万张时才不会把连接池打满。
  ② **骨架占位**：格子尺寸靠 aspect-ratio 预留，所以图片到位时**不产生回流**，
     列表不会边滚边跳。`contain-intrinsic-size` 配合 content-visibility 使用。
  ③ **角标淡入**：图片 load 之后角标才淡入（120ms），避免"先看到角标、
     再看到照片"的错位感。角标**常驻**（不是 hover 才出）—— 它承载的是
     「有几张脸 / 是不是重复 / 有没有 GPS」，是需要被扫读的信息。
-->
<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Camera, Copy, MapPin, TriangleAlert } from 'lucide-vue-next'
import { placeDisplayName, placeRawName } from '@/api/place'
import { thumbUrl } from '@/api/static'
import { baseName, formatYear } from '@/utils/format'

const props = defineProps({
  /** pb_photo 派生的一行（photoSummary 形状） */
  photo: { type: Object, required: true },
  /** square = 1:1（网格默认）；fourThree = 4:3 */
  ratio: { type: String, default: 'square' },
  /** 首屏前几张直接给 src，跳过一次 IO 往返 */
  eager: { type: Boolean, default: false },
  /** 底部是否显示文件名（时间轴视图要，网格不要） */
  showName: { type: Boolean, default: false },
  /** 格子尺寸下的缩略图档位：200 / 400 */
  thumbSize: { type: Number, default: 200 },
})

const emit = defineEmits(['open', 'error'])

const host = ref(null)
/** 视口内（或首屏 eager）才为 true —— 决定要不要真正发请求 */
const shouldLoad = ref(props.eager)
const loaded = ref(false)
const failed = ref(false)

let observer = null

function ensureObserver() {
  if (observer || typeof IntersectionObserver === 'undefined') {
    // 老浏览器没有 IO：退化成"直接加载"，宁可多请求也不能不显示
    if (!observer) shouldLoad.value = true
    return
  }
  observer = new IntersectionObserver(
    (entries) => {
      if (entries.some((entry) => entry.isIntersecting)) {
        shouldLoad.value = true
        stopObserver()
      }
    },
    // 提前 500px 开始加载：用户滚到那里之前图已经在路上
    { rootMargin: '500px 0px' },
  )
  if (host.value) observer.observe(host.value)
}

function stopObserver() {
  if (observer) {
    observer.disconnect()
    observer = null
  }
}

onMounted(() => {
  if (props.eager) {
    shouldLoad.value = true
    return
  }
  ensureObserver()
})

onBeforeUnmount(stopObserver)

// 换照片（列表被就地替换）时重置加载态
watch(
  () => props.photo?.photoCode,
  () => {
    loaded.value = false
    failed.value = false
    if (props.eager) shouldLoad.value = true
    else ensureObserver()
  },
)

const ratioClass = computed(() =>
  props.ratio === 'fourThree' ? 'aspect-[4/3]' : 'aspect-square',
)

/** 只可能来自 /api/thumb —— 见文件头红线 */
const src = computed(() => {
  const code = props.photo?.photoCode
  if (!code || !shouldLoad.value) return ''
  return thumbUrl(code, props.thumbSize)
})

/**
 * DR-43：人工旋转角度（0/90/180/270）。
 *
 * 这里**只转 <img>**，因为格子恒为正方形（`aspect-square`，实测全项目没有一处
 * 传 `ratio="fourThree"`）—— 正方形转 90° 还是正方形，**没有空隙**，不需要
 * 外层换比例，也不需要 scale 补边。
 * ⚠️ **角标（👤/⚠/📷）与年份底衬、失败占位都不许转** —— 它们与照片内容无关，
 *    转了只会变成斜的。所以 transform 加在 img 上，不加在 frame 上。
 * ⚠️ `object-cover` 是「先按正方形裁、再整体旋转」，与「先旋转、再按正方形裁」
 *    取到的区域**不完全相同**（正方形旋转不变，所以不会有空隙，只是取景范围
 *    略有差别）。缩略图尺度上可接受 —— **不要为此去改缓存或做双份生成**。
 */
const rotateDeg = computed(() => ((Number(props.photo?.rotateDeg) || 0) % 360 + 360) % 360)
const imgStyle = computed(() =>
  rotateDeg.value ? { transform: `rotate(${rotateDeg.value}deg)` } : {},
)

const faceCount = computed(() => Number(props.photo?.faceCount) || 0)
const hasGps = computed(() => Boolean(props.photo?.hasGps))
const isDuplicate = computed(() => Number(props.photo?.isDuplicate) === 1)
const isMissing = computed(() => Number(props.photo?.isMissing) === 1)

/**
 * 无障碍文案。
 * ⚠️ 地点用 `placeDisplayName()`（中文优先，R5）：读屏用户听到的应当是
 *    「华盛顿」而不是 `--` 或英文键；地点是**扫读**这张照片的关键信息之一。
 * ⚠️ 判据是「有地点」而不是「有 GPS」：598 张目录名照片没有 GPS 但有地点。
 */
const placeText = computed(() =>
  (props.photo?.placeName || props.photo?.placeZh) ? placeDisplayName(props.photo) : '',
)

/** 缩略图上的 GPS 角标 tooltip：带上地点中文名（没有地点时退回通用文案） */
const gpsTitle = computed(() => {
  if (!hasGps.value) return ''
  return placeText.value ? `有 GPS 定位：${placeText.value}` : '有 GPS 定位'
})

/** 地点原值（tooltip 用，排障） */
const placeTitle = computed(() => placeRawName(props.photo))

const altText = computed(() => {
  const parts = [baseName(props.photo?.relPath) || '照片']
  const year = formatYear(props.photo)
  if (year !== '—') parts.push(`${year} 年`)
  if (placeText.value) parts.push(placeText.value)
  if (faceCount.value) parts.push(`${faceCount.value} 张人脸`)
  if (isDuplicate.value) parts.push('重复照片')
  if (isMissing.value) parts.push('原图不在磁盘上')
  if (hasGps.value) parts.push('有 GPS')
  return parts.join('，')
})

function onLoad() {
  loaded.value = true
}

function onError() {
  // 404 = 原图不在磁盘（拔盘/挪走）；422 = 原图解不出像素（文件本身坏了）。
  // 两种都**不是**服务端的错，所以这里显示占位而不是弹错误。
  loaded.value = false
  failed.value = true
  emit('error', { photo: props.photo, failed: true })
}
</script>

<template>
  <figure ref="host" class="group block">
    <div
      class="pb-photo-frame relative block overflow-hidden transition-transform duration-[120ms] ease-out will-change-transform group-hover:-translate-y-0.5"
      :class="[ratioClass, failed ? 'bg-danger-soft' : '']"
    >
      <!-- 骨架占位：图片 load 后才撤 -->
      <div
        v-if="!loaded && !failed"
        class="absolute inset-0 animate-pulse bg-skeleton"
        aria-hidden="true"
      />

      <img
        v-if="src && !failed"
        :src="src"
        :alt="altText"
        class="block h-full w-full object-cover"
        :style="imgStyle"
        decoding="async"
        draggable="false"
        @load="onLoad"
        @error="onError"
      />

      <!-- 失败占位：说清楚是哪一类失败 -->
      <div
        v-if="failed"
        class="absolute inset-0 flex flex-col items-center justify-center gap-1 p-2 text-center text-caption text-danger-ink"
      >
        <TriangleAlert class="h-4 w-4" aria-hidden="true" />
        <span>{{ isMissing ? '原图不在磁盘上' : '缩略图生成失败' }}</span>
      </div>

      <!-- 角标：图标 + 文字（不只靠颜色），图片到位后淡入 -->
      <div
        class="pointer-events-none absolute inset-x-0 top-0 flex items-start gap-1 p-1 transition-opacity duration-[120ms] ease-out"
        :class="loaded ? 'opacity-100' : 'opacity-0'"
      >
        <span
          v-if="faceCount"
          class="inline-flex items-center gap-0.5 rounded-btn bg-card px-1 py-0.5 text-caption text-ink"
          :title="`含 ${faceCount} 张人脸`"
        >
          <Camera class="h-3 w-3" aria-hidden="true" />
          <span class="tabular-nums">{{ faceCount }}</span>
          <span class="sr-only">张人脸</span>
        </span>
        <span
          v-if="isDuplicate"
          class="inline-flex items-center gap-0.5 rounded-btn bg-warning-soft px-1 py-0.5 text-caption text-warning-ink"
          title="重复照片"
        >
          <Copy class="h-3 w-3" aria-hidden="true" />
          重复
        </span>
        <span
          v-if="hasGps"
          class="inline-flex items-center gap-0.5 rounded-btn bg-card px-1 py-0.5 text-caption text-ink"
          :title="gpsTitle"
        >
          <MapPin class="h-3 w-3" aria-hidden="true" />
          <span class="sr-only">{{ gpsTitle }}</span>
        </span>
      </div>

      <!-- 拍摄年份：常驻在底部（hover 时加深底衬，不加任何滤镜） -->
      <div
        class="pointer-events-none absolute inset-x-0 bottom-0 px-1.5 pb-1 pt-3 text-caption text-ink-invert"
        :class="loaded ? 'opacity-100' : 'opacity-0'"
        style="background: linear-gradient(transparent, rgba(0, 0, 0, 0.55))"
      >
        <span class="tabular-nums">{{ formatYear(photo) }}</span>
      </div>
    </div>

    <figcaption
      v-if="showName"
      class="mt-1 truncate text-caption text-ink-weak"
      :title="photo?.relPath"
    >
      {{ baseName(photo?.relPath) }}
      <!-- 有地点时把**中文名**补在文件名后面（时间轴视图里要靠它认地方） -->
      <span v-if="placeText" class="text-ink-sub" :title="placeTitle || undefined">
        · {{ placeText }}
      </span>
    </figcaption>
  </figure>
</template>
