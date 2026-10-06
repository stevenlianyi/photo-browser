// Tailwind 配置 —— 步骤 10「浅色淡雅 + 跟随系统」双主题的**唯一映射表**
// （ESM 配置文件，同样不能加 `#!` shebang，理由见 vite.config.js）
//
// 设计约定（见 plan/UI/photo-browser UI 设计.md 第七节）：
//   · 所有颜色一律引用 src/styles/tokens.css 里的 CSS 变量，Tailwind 里不写死 hex，
//     这样切主题时不需要重新构建 CSS，只换 <html> 上的 class。
//   · ⚠️ 因为颜色值是裸 var()（不是 `rgb(var(--x) / <alpha-value>)` 形式），
//     **不能使用透明度修饰符**（如 `bg-brand/10` 会失效）。需要淡底时用配套的
//     `-soft` 语义 token（如 `bg-brand-soft`、`bg-success-soft`）。
//   · 主题类由 src/utils/theme.js 挂到 <html>：`.dark` / `.light` + `data-theme-mode`。
import defaultTheme from 'tailwindcss/defaultTheme'

/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{vue,js}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // ---- 中性面 ----
        bg: 'var(--pb-bg)',
        surface: 'var(--pb-surface)',
        card: 'var(--pb-card)',
        line: 'var(--pb-border)',
        'line-strong': 'var(--pb-border-strong)',
        // ---- 品牌 ----
        brand: {
          DEFAULT: 'var(--pb-primary)',
          strong: 'var(--pb-primary-strong)',
          soft: 'var(--pb-primary-soft)',
          ink: 'var(--pb-primary-ink)',
        },
        // ---- 文本 ----
        ink: 'var(--pb-text)',
        'ink-sub': 'var(--pb-text-sub)',
        'ink-weak': 'var(--pb-text-weak)',
        'ink-invert': 'var(--pb-text-invert)',
        // ---- 语义色：图形/填充 ----
        success: 'var(--pb-success)',
        warning: 'var(--pb-warning)',
        danger: 'var(--pb-danger)',
        info: 'var(--pb-info)',
        accent: 'var(--pb-accent)',
        // ---- 语义色：文字安全版（WCAG AA，实测对比度见 tokens.css 注释）----
        'success-ink': 'var(--pb-success-ink)',
        'warning-ink': 'var(--pb-warning-ink)',
        'danger-ink': 'var(--pb-danger-ink)',
        'info-ink': 'var(--pb-info-ink)',
        // ---- 语义色：浅底（徽标/标签背景）----
        'success-soft': 'var(--pb-success-soft)',
        'warning-soft': 'var(--pb-warning-soft)',
        'danger-soft': 'var(--pb-danger-soft)',
        'info-soft': 'var(--pb-info-soft)',
        'accent-soft': 'var(--pb-accent-soft)',
        // ---- 其它 ----
        photo: 'var(--pb-photo-bg)',
        skeleton: 'var(--pb-skeleton)',
      },
      fontFamily: {
        // 本机离线工具，不拉 Google Fonts；Inter 只在用户本地装有时命中。
        sans: ['Inter', 'PingFang SC', 'Microsoft YaHei', ...defaultTheme.fontFamily.sans],
        mono: ['JetBrains Mono', 'Consolas', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
      fontSize: {
        // 设计稿：标题 24/600 ｜ 副标题 16/600 ｜ 正文 14/400
        title: ['24px', { lineHeight: '32px', fontWeight: '600' }],
        subtitle: ['16px', { lineHeight: '24px', fontWeight: '600' }],
        body: ['14px', { lineHeight: '22px', fontWeight: '400' }],
        caption: ['12px', { lineHeight: '18px', fontWeight: '400' }],
      },
      borderRadius: {
        // 设计稿：卡片 12 / 按钮 8 / 缩略图 8
        card: '12px',
        btn: '8px',
        thumb: '8px',
      },
      boxShadow: {
        // 极轻阴影；深色主题下 --pb-shadow-* 走「无阴影改用边框」路线
        card: 'var(--pb-shadow-card)',
        pop: 'var(--pb-shadow-pop)',
      },
      spacing: {
        // 4px 基准（Tailwind 默认即 4px 基准，这里只补语义名）
        topbar: '56px',
        sidebar: '220px',
        'sidebar-mini': '72px',
      },
      maxWidth: {
        content: '1600px',
      },
      screens: {
        // 桌面基准 1440；<1024 侧栏收窄为图标；<768 布局转上下
        sm: '640px',
        md: '768px',
        lg: '1024px',
        xl: '1280px',
        '2xl': '1536px',
      },
    },
  },
  plugins: [],
}