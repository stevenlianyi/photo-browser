/* ============================================================
 * 主题三态切换 —— 浅色 / 深色 / 跟随系统
 * ============================================================
 * 落点只有一个：<html> 上的 class 与 data 属性
 *   .dark           → tokens.css 深色色板 + element-theme.css 深色 --el-*
 *   .light          → 强制浅色（系统深色时也保持浅色）
 *   data-theme-mode → 'light' | 'dark' | 'auto'，供排障与自动化断言
 *
 * 首屏防闪：index.html <head> 的内联脚本先跑同样的判定并打好 class，
 * 本模块随后再跑一次（幂等）。⚠️ 两处逻辑必须一致，改 resolveDark() 时
 * 记得同步 index.html。
 *
 * 持久化：localStorage['pb-theme-mode']，**三态都存**（不是只存深浅），
 * 否则「跟随系统」刷新后会退化成上一次的强制结果。
 * ============================================================ */
import { ref } from 'vue'

/** localStorage 键名 */
export const THEME_STORAGE_KEY = 'pb-theme-mode'

export const THEME_LIGHT = 'light'
export const THEME_DARK = 'dark'
export const THEME_AUTO = 'auto'

/** 合法取值白名单 + 展示信息（TopBar 开关直接读它，不写第二份文案） */
export const THEME_MODES = [THEME_LIGHT, THEME_DARK, THEME_AUTO]

export const THEME_META = {
  [THEME_LIGHT]: { label: '浅色', short: '浅', hint: '始终使用浅色界面' },
  [THEME_DARK]: { label: '深色', short: '深', hint: '始终使用深色界面' },
  [THEME_AUTO]: { label: '跟随系统', short: '随', hint: '跟随操作系统的深浅色设置' },
}

/** 默认值：**浅色**（设计稿 §7.4「默认浅色」，不是跟随系统） */
export const DEFAULT_THEME_MODE = THEME_LIGHT

const DARK_QUERY = '(prefers-color-scheme: dark)'

/** 当前模式（响应式，组件里直接用） */
export const themeMode = ref(DEFAULT_THEME_MODE)
/** 解析后的实际生效主题（true = 深色） */
export const isDarkTheme = ref(false)

/** localStorage 不可用时（隐私模式）静默降级，不抛错打断首屏 */
function readStoredMode() {
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY)
    return THEME_MODES.includes(stored) ? stored : null
  } catch (error) {
    return null
  }
}

function persistMode(mode) {
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, mode)
  } catch (error) {
    /* 存不了就算了，本次会话内仍然生效 */
  }
}

/** 系统当前是否深色 */
export function systemPrefersDark() {
  return Boolean(window.matchMedia && window.matchMedia(DARK_QUERY).matches)
}

/** 三态 → 实际深浅（与 index.html 内联脚本同逻辑） */
export function resolveDark(mode) {
  if (mode === THEME_DARK) return true
  if (mode === THEME_AUTO) return systemPrefersDark()
  return false
}

/** 把判定结果写进 DOM（幂等，可重复调用） */
export function applyTheme(mode = themeMode.value) {
  const safeMode = THEME_MODES.includes(mode) ? mode : DEFAULT_THEME_MODE
  const dark = resolveDark(safeMode)
  const root = document.documentElement

  root.classList.toggle('dark', dark)
  root.classList.toggle('light', !dark)
  root.setAttribute('data-theme-mode', safeMode)

  themeMode.value = safeMode
  isDarkTheme.value = dark
  return dark
}

/** 对外入口：三态切换（写 localStorage + 更新 DOM） */
export function setThemeMode(mode) {
  if (!THEME_MODES.includes(mode)) return themeMode.value
  persistMode(mode)
  return applyTheme(mode)
}

/** 三态轮转（浅 → 深 → 跟随系统 → 浅），给键盘快捷键用 */
export function nextThemeMode(from = themeMode.value) {
  const index = THEME_MODES.indexOf(from)
  return setThemeMode(THEME_MODES[(index + 1) % THEME_MODES.length])
}

/** 首屏初始化：读 localStorage（无则默认浅色）并挂到 <html> */
export function initTheme() {
  const stored = readStoredMode()
  return applyTheme(stored || DEFAULT_THEME_MODE)
}

/**
 * 监听系统深浅变化。
 * ⚠️ 只有「跟随系统」态才跟随；手动浅/深是用户的明确选择，不能被系统变化覆盖
 * （这是三态开关与两态开关最本质的区别）。
 * @returns {Function} 解绑函数
 */
export function watchSystemPreference() {
  if (!window.matchMedia) return () => {}
  const media = window.matchMedia(DARK_QUERY)
  const onChange = () => {
    if (themeMode.value === THEME_AUTO) applyTheme(THEME_AUTO)
  }
  if (media.addEventListener) {
    media.addEventListener('change', onChange)
    return () => media.removeEventListener('change', onChange)
  }
  media.addListener(onChange)
  return () => media.removeListener(onChange)
}