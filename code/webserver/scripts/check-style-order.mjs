/* ============================================================
 * check-style-order —— CSS 覆盖顺序门禁（npm run build 后自动跑）
 * ============================================================
 * 守住的不变量：**EP 的默认 --el-* 变量不能赢过我们的覆盖**。
 *
 * 为什么需要门禁（这不是防御性编程，是有具体故障史的）
 * --------------------------------------------------------
 *   我们有两层「覆盖」：element-theme.css（改 --el-* 变量）与 main.css
 *   （Tailwind 三层 + .pb-* 工具类）。它们必须排在 EP 之后，顺序一颠倒
 *   症状是「改了 tokens.css / element-theme.css 完全不生效」——
 *   而且**不会报错**：CSS 合法、构建通过、页面照常显示，只是悄悄变回 EP 蓝。
 *   人肉 review 这种顺序最容易漏，因为它「看起来只是 import 位置」。
 *
 * ⚠️ 为什么必须「标记找不到就报错」而不是跳过
 *   下面这些标记串依赖压缩器输出（十六进制小写、`--x: v` 的空格、var() 内联），
 *   升级 Vite / esbuild / lightningcss 后标记可能整体挪位。
 *   那时如果脚本「找不到就当过」，它就变成一个永远绿的假保险 ——
 *   比没有门禁更糟。���以**找不到标记 = 直接失败**，并在报错里告诉维护者
 *   该改哪一行。这也是本脚本不用任何第三方 CSS 解析器的原因：
 *   纯字符串定位，工具链升级时最多改 MARKERS 常量，不会连带解析逻辑一起崩。
 *
 * 断言分两层
 * ------------------------------------------------------------
 *   第1 层（源码）：src/main.js 的 import 次序。完全由我们控制，不受压缩影响。
 *   第 2 层（产物）：入口 CSS 内部的先后 + 「最后一次定义是我们」。
 *                    这一层才拦得住「构建工具改了 CSS 拼接顺序」这种我们控制不了的变化。
 */

import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const failures = []

function check(ok, message, hint) {
  if (ok) return true
  failures.push(hint ? `${message}\n    ↳ ${hint}` : message)
  return false
}

// ------------------------------------------------------------
// 第 1 层：src/main.js 的 import 次序
// ------------------------------------------------------------
const MAIN_JS = join(ROOT, 'src', 'main.js')
const SOURCE_ORDER = [
  ['tokens.css（--pb-* 唯一来源）', /styles\/tokens\.css/],
  ['EP base.css（--el-* 默认值）', /theme-chalk\/base\.css/],
  ['EP dark/css-vars.css（html.dark 默认值）', /dark\/css-vars\.css/],
  ["App.vue（EP 按需组件样式从它这里进图）", /from ['"]\.\/App\.vue['"]/],
  ['element-theme.css（用 --pb-* 覆盖 --el-*）', /styles\/element-theme\.css/],
  ['main.css（Tailwind 三层，必须最后）', /styles\/main\.css/],
]

if (!existsSync(MAIN_JS)) {
  console.error('[style-order] 找不到 src/main.js，脚本应在 code/webserver 下运行')
  process.exit(1)
}

// ⚠️ 先剥掉注释再找位置：main.js 的文件头**恰好按顺序列出了这几个文件**
//   （那是给人看的说明）。不剥注释的话第一处匹配会落在注释里，
//   门禁会报一个根本不存在的「次序错误」—— 假警报比没有门禁更费时间。
const mainSource = readFileSync(MAIN_JS, 'utf8')
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .replace(/^\s*\/\/.*$/gm, '')

let lastIndex = -1
let lastName = '(起点)'
for (const [name, pattern] of SOURCE_ORDER) {
  const at = mainSource.search(pattern)
  if (at < 0) {
    check(false, `src/main.js 里找不到「${name}」`, 'import 被删掉或改了路径，请同步本脚本的 SOURCE_ORDER')
    continue
  }
  check(
    at > lastIndex,
    `src/main.js 的 import 次序错了：「${name}」出现在「${lastName}」之前`,
    '覆盖层必须排在 EP 之后（见 main.js 文件头的顺序说明）',
  )
  if (at > lastIndex) {
    lastIndex = at
    lastName = name
  }
}

// ------------------------------------------------------------
// 第 2 层：入口 CSS 内部次序
// ------------------------------------------------------------
const ASSETS = join(ROOT, 'dist', 'assets')
if (!existsSync(ASSETS)) {
  console.error('[style-order] 找不到 dist/assets —— 请先 npm run build（postbuild 应在本脚本之前执行）')
  process.exit(1)
}

// 入口 CSS = 含 --pb-bg 的那个（tokens 只被打进入口）
const cssFiles = readdirSync(ASSETS).filter((name) => name.endsWith('.css'))
let entryName = ''
let entryCss = ''
for (const name of cssFiles) {
  const text = readFileSync(join(ASSETS, name), 'utf8')
  if (text.includes('--pb-bg:')) {
    entryName = name
    entryCss = text
    break
  }
}
if (!entryName) {
  console.error('[style-order] dist/assets 里没有任何 CSS 含 --pb-bg：tokens.css 没打进入口，检查 main.js 的样式 import')
  process.exit(1)
}

// 压缩后的实际写法（十六进制小写、自定义属性后带空格、var() 可能被内联）
const MARKERS = [
  ['tokens.css：--pb-bg', '--pb-bg:'],
  ['EP base.css 默认主色', '--el-color-primary:#409eff'],
  ['element-theme.css 覆盖主色', '--el-color-primary: #5b8def'],
  ['main.css：.pb-page', '.pb-page{'],
]

let prevIndex = -1
let prevName = '(起点)'
for (const [name, marker] of MARKERS) {
  const at = entryCss.indexOf(marker)
  if (!check(at >= 0, `入口 CSS ${entryName} 里找不到标记「${name}」（${marker}）`, '压缩器输出变了：改脚本顶部 MARKERS 里的标记串，不要跳过断言')) continue
  check(
    at > prevIndex,
    `入口 CSS 次序错了：「${name}」出现在「${prevName}」之前`,
    'EP 默认值赢了过我们的覆盖（症状：整个界面变回 EP 蓝）',
  )
  if (at > prevIndex) {
    prevIndex = at
    prevName = name
  }
}

// 最强的一条：**EP 的默认值一次都不许出现在我们的覆盖之后**。
// ⚠️ 不能写成「最后一次定义必须是 #5b8def」—— 深色块的 #7ba6f5 合法地排在后面，
//   那是html.dark 里的覆盖，不是「有人把主色改回去」。
//   真正要拦的是：EP 默认值（#409eff）出现在我们的覆盖**之后**，
//   那它会在等特异度下赢过 element-theme.css，整个界面变回 EP 蓝。
const allPrimary = []
const rePrimary = /--el-color-primary:[^;}]*/g
let hit = rePrimary.exec(entryCss)
while (hit) {
  allPrimary.push({ index: hit.index, text: hit[0] })
  hit = rePrimary.exec(entryCss)
}

const ourLight = entryCss.indexOf('--el-color-primary: #5b8def')
const ourDark = entryCss.indexOf('--el-color-primary: #7ba6f5')
const epDefaults = allPrimary.filter((item) => item.text.indexOf('#409eff') >= 0)

check(epDefaults.length > 0, '入口 CSS 里找不到 EP 默认主色 #409eff，无法判断覆盖关系', 'EP 版本可能变了，更新脚本里的标记串')
check(ourLight >= 0, '入口 CSS 里找不到我们的浅色主色覆盖 #5b8def', 'element-theme.css 可能没被打包，或变量名变了')
check(ourDark >= 0, '入口 CSS 里找不到我们的深色主色覆盖 #7ba6f5', '深色主题的主色覆盖丢了（深色下会退回 EP 蓝）')

for (const item of epDefaults) {
  check(
    ourLight < 0 || item.index < ourLight,
    `EP 默认主色出现在我们的覆盖之后（位置 ${item.index} vs ${ourLight}）`,
    '等特异度 + 后加载 = EP 默认值赢，界面会变回 EP 蓝',
  )
}

// ------------------------------------------------------------
// 结果
// ------------------------------------------------------------
if (failures.length > 0) {
  console.error('[style-order] 样式覆盖顺序门禁未通过：')
  for (const item of failures) console.error(`  ✗ ${item}`)
  console.error('[style-order] 这类问题不报错、只表现为「改了主题变量不生效」，所以必须在构建期拦住。')
  process.exit(1)
}

console.log(
  `[style-order] OK（入口 CSS: ${entryName}；--el-color-primary 共 ${allPrimary.length} 处定义，` +
    `EP 默认 #409eff 全部位于我们的覆盖之前）`,
)