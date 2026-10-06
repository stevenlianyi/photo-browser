// photo-browser 前端构建配置（ESM；⚠️ 不能加 `#!` shebang ——
// Vite 会把配置打包进临时文件再执行，shebang 会被挤到文件中间变成语法错误）
//
// 版本矩阵（只写大版本 —— 写死小版本意味着每次升依赖都要改注释，注释一旦过期
// 就会变成误导。这里记的是**约束来源**，不是某次安装的结果）
//   Node   22.x（>=22.12）—— Vite 7 的硬要求；element-plus 的 @vueuse/* 也要求 >=22
//   npm    10.x
//   Vite   7.x
//   Tailwind 3.4.x —— **不能用 v4**：配置方式完全不同（CSS-first，无 tailwind.config.js）
//
// Vite 6→7 迁移对本项目的影响（都已实测确认）：
//   · 产物更小（主 chunk 251 kB → 247 kB），tree-shaking 略强
//   · **CSS 覆盖顺序不变**：element-theme.css / main.css 仍然赢过 EP 组件样式
//     （实测 el-table 表头底色 = --pb-surface 而非 EP 默认色）。若哪天顺序翻了，
//     症状是「改了 --pb-* 不生效」，先回来查main.js 里的 import 次序
//   · Node API 的 CJS 入口已移除 —— 本项目是纯 ESM（package.json type=module），无影响
import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

export default defineConfig({
  plugins: [
    vue(),
    // Element Plus 按需引入：模板里的 <el-button> 等自动注册 + 自动引入对应样式，
    // 不再全量 app.use(ElementPlus)（那会把整个 EP 打进主 chunk）。
    // ⚠️ 副作用：EP 的样式改为「在用到它的 .vue 里注入」，所以 main.js 必须把
    //    element-theme.css / main.css 排在 App.vue 之后 import —— 见 main.js 的注释。
    Components({
      resolvers: [ElementPlusResolver({ importStyle: 'css' })],
      dts: false,
    }),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: false,
    // 允许通过局域网主机名/容器主机名访问 dev server。
    // 依赖 Vite ≥5.4.12 的安全补丁版本才支持 `true`（5.4.12 之前只接受字符串数组）。
    allowedHosts: true,
  },
  preview: {
    host: '127.0.0.1',
    port: 4173,
  },
  build: {
    // 构建产物由后端 main/app.py 以 StaticFiles(html=True) 挂在 / 上，故 base 用相对路径。
    outDir: 'dist',
    emptyOutDir: true,
    // 收紧到 600 kB：按需引入之后主chunk 只有 ~247 kB，阈值还留在 1500 的话，
    // 哪天有人把app.use(ElementPlus) 加回来（主 chunk 会涨到 1.1 MB）也不会报警。
    // 阈值定在「明显超出当前量级」而不是「刚好装得下」，超标要能被发现。
    chunkSizeWarningLimit: 600,
  },
})