<!--
  主题三态开关（浅色 / 深色 / 跟随系统）
  ------------------------------------------------------------
  为什么是三个按钮而不是一个「亮/暗」开关：
  「跟随系统」必须是一个**显式可选的稳定状态**。两态开关在系统切深色时会被覆盖，
  用户想「我就用浅色」却说不出来。三个按钮 = 三个可回读的稳定状态。

  无障碍：用 button + aria-pressed，不用 el-switch（开关控件表达不了三态）
  键盘：Tab 可达、Enter/Space 可触发，焦点环由 main.css 的 :focus-visible 提供
-->
<script setup>
import { Monitor, Moon, Sun } from 'lucide-vue-next'
import { THEME_META, THEME_MODES, setThemeMode, themeMode } from '@/utils/theme'

const ICONS = { light: Sun, dark: Moon, auto: Monitor }

function pick(mode) {
  if (mode !== themeMode.value) setThemeMode(mode)
}
</script>

<template>
  <div
    class="inline-flex shrink-0 items-center gap-0.5 rounded-btn border border-line bg-card p-0.5"
    role="group"
    aria-label="主题外观"
  >
    <button
      v-for="mode in THEME_MODES"
      :key="mode"
      type="button"
      class="inline-flex h-7 items-center gap-1.5 rounded-btn px-2 text-caption transition-colors duration-150"
      :class="
        themeMode === mode
          ? 'bg-brand-soft text-brand-ink'
          : 'text-ink-sub hover:bg-surface hover:text-ink'
      "
      :aria-pressed="themeMode === mode"
      :aria-label="`主题：${THEME_META[mode].label}。${THEME_META[mode].hint}`"
      :title="`${THEME_META[mode].label} —— ${THEME_META[mode].hint}`"
      @click="pick(mode)"
    >
      <component :is="ICONS[mode]" class="h-4 w-4 shrink-0" aria-hidden="true" />
      <!-- 窄屏（<1280）只留图标，文字仍在 aria-label 里，读屏与悬停提示都不丢 -->
      <span class="hidden whitespace-nowrap xl:inline">{{ THEME_META[mode].label }}</span>
    </button>
  </div>
</template>