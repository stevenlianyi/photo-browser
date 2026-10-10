/* ============================================================
 * 头像三级回退的**可执行**校验（`npm run check:avatar`，也在 postbuild 里）
 * ============================================================
 * 为什么必须是「真的执行」而不是看代码
 * ----------------------------------
 *   这条链上的错误**不会报错、不会破图**，只会安静地退化：
 *   整页头像变成灰圆、或者变成「?」—— 而这个坑在人物卡片 / 照片详情 /
 *   改判浮层 / 通讯录头像上已经反复出现过 **四次**。
 *   最近一次就是「getter 没被调用」：`unref(() => props.person)` 拿到的是
 *   那个**函数对象**，`fn.displayName` 是 undefined ⇒ 首字母退化成 `?`，
 *   而代码看上去完全正常。
 *
 * 怎么跑：`npm run check:avatar`
 *   utils/avatar.js 里有 `@/api/static` 这类别名，Node 直接 import 不了，
 *   所以先让 vite 以 **ssr** 模式把本文件打成 Node 可执行模块（产物在
 *   dist-ssr/，已在 .gitignore 里），再 node 跑它。
 *   `vue` 是外部依赖（不打包），Node 从 node_modules 解析。
 */
import { nextTick, ref } from 'vue'
import {
  avatarFaceCodeOf,
  avatarSourcesOf,
  initialOf,
  useAvatarSources,
} from '../src/utils/avatar.js'

let failed = 0
function check(name, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want)
  if (!ok) failed += 1
  console.log('%s %s -> %s%s', ok ? '[ok]  ' : '[FAIL]', name, JSON.stringify(got),
    ok ? '' : '（期望 ' + JSON.stringify(want) + '）')
}

const person = {
  personCode: 'CS_gong',
  displayName: '龚大军',
  coverFaceCode: 'fcAAA',
  avatarFaceCode: null,
  contactAvatarUrl: '/api/avatar/CS_gong',
}

// ---- 纯函数 ----
check('avatarFaceCodeOf 优先 coverFaceCode', avatarFaceCodeOf(person), 'fcAAA')
check('avatarFaceCodeOf 退到队列形状的 avatarFaceCode',
  avatarFaceCodeOf({ avatarFaceCode: 'fcBBB' }), 'fcBBB')
check('avatarSourcesOf 顺序 = 人脸图 → 通讯录头像', avatarSourcesOf(person),
  ['/api/face/fcAAA', '/api/avatar/CS_gong'])
check('avatarSourcesOf 无脸时只剩通讯录头像',
  avatarSourcesOf({ personCode: 'P', contactAvatarUrl: '/api/avatar/P' }),
  ['/api/avatar/P'])
check('initialOf 取名字第一个字', initialOf(person), '龚')
check('initialOf 无名字时用 personCode 首字符', initialOf({ personCode: 'CS_x' }), 'C')

// ---- 状态机：**getter 形态**（各组件就是这么传的，曾经在这里退化成 ?） ----
const a = useAvatarSources(() => person)
check('getter: 有图', a.showImage.value, true)
check('getter: 第一级是人脸图', a.src.value, '/api/face/fcAAA')
check('getter: 首字母不是 ?', a.initial.value, '龚')

a.onError()                                   // 人脸裁剪图 404
check('人脸图失败 -> 退到通讯录头像', a.src.value, '/api/avatar/CS_gong')
a.onError()                                   // 通讯录头像也 404
check('两级都失败 -> 让位给首字母', a.src.value, '')
check('此时 showImage=false', a.showImage.value, false)
check('此时首字母仍是名字首字（曾经是 ?）', a.initial.value, '龚')

// ---- 没脸也没通讯录头像：直接首字母 ----
const b = useAvatarSources(() => ({ personCode: 'CS_x', displayName: '齐晓枫' }))
check('无任何图 -> 不显示 img', b.showImage.value, false)
check('无任何图 -> 首字母', b.initial.value, '齐')

// ---- 换人（ref 形态）：要重头再试，不能锁死在上一级 ----
const current = ref({ personCode: 'P1', displayName: '甲', coverFaceCode: 'fc1' })
const c = useAvatarSources(() => current.value)
check('ref: 初始人脸图', c.src.value, '/api/face/fc1')
c.onError()
check('ref: 失败后退到末尾（无图）', c.src.value, '')
current.value = { personCode: 'P2', displayName: '乙', coverFaceCode: 'fc2' }
await nextTick()
await nextTick()
check('换人后重新从人脸图开始', c.src.value, '/api/face/fc2')
check('换人后首字母跟着换', c.initial.value, '乙')

console.log(failed ? '\n失败 %d 条' % failed : '\n全部通过')
process.exit(failed ? 1 : 0)
