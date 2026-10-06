/*! photo-browser 前端入口
 *
 * 职责：① 装配 Vue / Pinia / Router
 *      ② 主题首屏同步（不闪白/闪黑）
 *      ③ 样式加载顺序（**顺序即优先级，不可颠倒**）
 *
 * 样式顺序说明（步骤 10 改按需引入后重排过一次，顺序变了两次原因）：
 *   tokens.css先定 --pb-*（颜色唯一来源）
 *   element-plus/theme-chalk/base.css        EP 默认 --el-*（:root）
 *   element-plus/theme-chalk/dark/css-vars.css   EP 官方深色（html.dark）
 *   ↓ App.vue（**在这里**递归带出所有 EP 组件样式与业务样式）
 *   element-theme.css用 --pb-* 覆盖 --el-*，必须在 EP 之后
 *   main.css                Tailwind 三层 + 全局基线，必须在 element-theme 之后
 *
 * ⚠️ 为什么 App.vue 必须夹在覆盖层中间
 *   按需引入后，EP 的 `.el-button` 等样式不再是 main.js 里那个整包 CSS，
 *   而是 unplugin-vue-components 在**各个 .vue 里**按需 import 的 ——
 *   注入时机由模块图决定。element-theme.css 与 main.css 都是「覆盖层」，
 *   必须在它们之后 import：EP 的表头底色、输入框圆角、按钮文字色都是同特异度覆盖，
 *   顺序一颠倒就会出现「改了 --pb-* 却不起作用」「Tailwind 类被 EP 压掉」。
 */
import { createApp } from 'vue'
import { createPinia } from 'pinia'

import './styles/tokens.css'
// base.css 提供了 EP 全局 --el-* 默认值：按需引入只带组件样式，不带这一层，
// 少了它 --el-font-size-* / --el-fill-color-* 等会全部 undefined，组件直接散架
import 'element-plus/theme-chalk/base.css'
import 'element-plus/theme-chalk/dark/css-vars.css'

import App from './App.vue'
import router from './router'
import { initTheme, watchSystemPreference } from './utils/theme'

// —— 以下两个必须排在 App.vue 之后（理由见文件头）——
import './styles/element-theme.css'
import './styles/main.css'

// 主题：读 localStorage 并同步到 <html>。index.html 已打过一次，这里保证
// 与 utils/theme.js 的状态一致（组件里读的 ref 也在这里被初始化）。
initTheme()
// 系统深浅变化时自动跟随（仅「跟随系统」态生效）
watchSystemPreference()

const app = createApp(App)

app.use(createPinia())
app.use(router)
// Element Plus 按需引入（unplugin-vue-components 自动注册），
// 组件的中文 locale 由 App.vue 里的 <el-config-provider> 提供 ——
// 不再 app.use(ElementPlus)，那会全量注册并把整个 EP 打进主 chunk。

app.mount('#app')