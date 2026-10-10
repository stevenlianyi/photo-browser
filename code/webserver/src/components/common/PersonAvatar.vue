<!--
  PersonAvatar —— 人物头像（圆形，带**三级回退**）
  ------------------------------------------------------------
  给「在 v-for 里逐个渲染头像」的列表用（改判浮层的候选/搜索结果、
  待确认队列、我不同意、地点在场的人…）。单个头像的页面
  （PersonCard / PersonDetailView 头部）直接用 `utils/avatar.js` 的
  `useAvatarSources()`，因为它们的圆框还带自己的装饰（停用态、默认头像环）。

  三级回退（顺序是产品口径，见 utils/avatar.js）
  --------------------------------------------
    ① 人脸裁剪图（coverFaceCode → avatarFaceCode）
    ② 通讯录头像（contactAvatarUrl，vCard 内嵌 PHOTO）
    ③ 姓名首字母
  每一级都接 `@error` 往后退 —— 服务端刻意不为列表探测文件系统，
  「图在不在盘上」只有浏览器加载失败时才知道。不退的话用户看到的是
  一个破图图标，比没有头像更难解释。

  ⚠️ 为什么要有这个组件（而不是各页各写一遍）
    同一个坑已经踩了三次（人物卡片 → 照片详情「出现的人」→ 改判浮层
    的搜索结果），每次都是「列表里一部分人有头像、其余是空圆」，
    而且**不报错**。头像的取值与降级只允许有一份实现。
-->
<script setup>
import { computed } from 'vue'
import { useAvatarSources } from '@/utils/avatar'

const props = defineProps({
  /** 人物对象：coverFaceCode / avatarFaceCode / contactAvatarUrl / displayName */
  person: { type: Object, required: true },
  /** 尺寸档：xs=24px sm=32px md=40px lg=64px xl=96px（类名必须字面写死，Tailwind 靠扫描源码） */
  size: { type: String, default: 'md' },
})

/** 尺寸 -> 圆框与首字母字号（表驱动，避免动态拼类名被 Tailwind 漏掉） */
const SIZES = {
  xs: { box: 'h-6 w-6', text: 'text-caption' },
  sm: { box: 'h-8 w-8', text: 'text-caption' },
  md: { box: 'h-10 w-10', text: 'text-body' },
  lg: { box: 'h-16 w-16', text: 'text-title' },
  xl: { box: 'h-24 w-24', text: 'text-title' },
}
const boxClass = computed(() => (SIZES[props.size] || SIZES.md).box)
const textClass = computed(() => (SIZES[props.size] || SIZES.md).text)

const { src, showImage, onError, initial } = useAvatarSources(() => props.person)
</script>

<template>
  <span
    class="flex shrink-0 items-center justify-center overflow-hidden rounded-full border border-line bg-surface"
    :class="boxClass"
    aria-hidden="true"
  >
    <img
      v-if="showImage"
      :src="src"
      class="h-full w-full object-cover"
      alt=""
      loading="lazy"
      decoding="async"
      @error="onError"
    />
    <span v-else class="text-ink-weak" :class="textClass">{{ initial }}</span>
  </span>
</template>
