<!--
  目录树（P-11「文件夹」页的左侧栏 · R16）
  ------------------------------------------------------------
  数据来自 `GET /api/albums/path` 的 `root`（**整棵树一次给全**）：
  展开/折叠纯前端做 —— 所以「切目录」不会让树重新请求、**展开状态也不会丢**
  （用户最烦的就是点一下目录、树自己合上）。也是这个原因，这里**不递归组件**，
  而是把可见节点**拍平成一维**再渲染：状态只有一份（`expanded`），
  缩进用 padding 表达 —— 比递归 + 逐层传状态好读，也更好做滚动定位。

  ⚠️ 两个数在界面上必须说清（本功能最容易让人误判的地方）
  --------------------------------------------------------
    · 节点上的张数是**直属**张数（不含子目录）—— 这正是「仅当前目录直属」的口径；
      含子目录的数在 `totalCount` 里，放在 title 里说明，不塞进节点行。
    · 所以「`2013美国游` 显示 3、它下面三个日期目录各几十张」**不是少报**，
      而是两个不同的量。点了它、看右侧网格就知道确实只有 3 张。
-->
<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import { ChevronDown, ChevronRight, Folder, FolderOpen } from 'lucide-vue-next'
import { formatCount } from '@/utils/format'

const props = defineProps({
  /** 根节点（`{name, path, count, totalCount, hasChildren, children}`） */
  root: { type: Object, default: null },
  /** 当前目录键（空串 = 根目录） */
  current: { type: String, default: '' },
})

const emit = defineEmits(['select'])

const host = ref(null)
/** 已展开的目录键。**整体替换**（而不是 mutate）才能保证 Set 变更被追踪 */
const expanded = ref(new Set())

/** 由外向内展开某个路径的全部祖先 —— 深链接/面包屑跳转时要把路径亮出来 */
function expandAlong(path) {
  const next = new Set(expanded.value)
  const text = String(path || '')
  if (!text) return
  const parts = text.split('/').filter(Boolean)
  let cursor = ''
  for (const segment of parts.slice(0, -1)) {
    cursor = cursor ? `${cursor}/${segment}` : segment
    next.add(cursor)
  }
  expanded.value = next
}

watch(() => props.current, (path) => {
  expandAlong(path)
  // 当前项滚动到可见区（树可能有几百项）。用 `block: 'nearest'`：
  // 已经在视口内时**不会**跳动 —— 否则每次点目录树都会自己滚一下。
  nextTick(() => {
    const node = host.value?.querySelector('[data-current="1"]')
    if (node && node.scrollIntoView) node.scrollIntoView({ block: 'nearest' })
  })
}, { immediate: true })

/** 拍平：只保留「自身 + 已展开祖先的后代」 */
const rows = computed(() => {
  const out = []
  const root = props.root
  if (!root) return out
  out.push({ node: root, depth: 0 })
  const walk = (list, depth) => {
    for (const node of list || []) {
      out.push({ node, depth })
      if (expanded.value.has(node.path)) walk(node.children, depth + 1)
    }
  }
  walk(root.children, 1)
  return out
})

function isOpen(node) {
  return expanded.value.has(node.path)
}

function toggle(node) {
  const next = new Set(expanded.value)
  if (next.has(node.path)) next.delete(node.path)
  else next.add(node.path)
  expanded.value = next
}

/**
 * 点行 = 进入该目录（并顺势展开）。
 * ⚠️ 展开与进入是**同一个动作**（与资源管理器一致）：只进入不展开的话，
 *   用户点了一个有子目录的目录，左侧看起来"什么都没发生"。
 */
function open(node) {
  emit('select', String(node.path || ''))
  if (node.hasChildren && !isOpen(node)) toggle(node)
}

function titleOf(node) {
  const direct = Number(node.count) || 0
  const total = Number(node.totalCount) || 0
  const name = node.name || '照片库'
  if (!node.hasChildren) return `${name}：${direct} 张（直属）`
  return `${name}：直属 ${direct} 张，含子目录共 ${total} 张`
}
</script>

<template>
  <nav
    ref="host"
    aria-label="目录树"
    class="pb-card max-h-[calc(100vh-13rem)] overflow-y-auto p-1.5"
  >
    <p v-if="!root" class="pb-hint px-2 py-3">正在读取目录…</p>

    <ul v-else class="space-y-0.5">
      <li v-for="row in rows" :key="row.node.path || '__root__'">
        <div
          class="group flex items-center rounded-btn transition-colors duration-150"
          :class="
            row.node.path === current
              ? 'bg-brand-soft text-brand-ink'
              : 'text-ink-sub hover:bg-surface hover:text-ink'
          "
          :data-current="row.node.path === current ? '1' : undefined"
        >
          <!-- 展开箭头（纯展开，不进入目录）：没有子目录时留一个等宽占位，
               否则同一层的目录名会因箭头有无而左右错开。
               ⚠️ 它**必须可键盘聚焦**（不加 `tabindex="-1"`）：`aria-expanded`
                  挂在这里，键盘用户要能 Tab 到它、回车展开 —— 否则屏幕阅读器
                  报不出"这个目录还能展开"，而鼠标用户看不出差别。 -->
          <button
            v-if="row.node.hasChildren"
            type="button"
            class="ml-0.5 flex h-6 w-5 shrink-0 cursor-pointer items-center justify-center rounded-btn text-ink-weak hover:text-ink"
            :aria-expanded="isOpen(row.node) ? 'true' : 'false'"
            :aria-label="`${isOpen(row.node) ? '折叠' : '展开'}目录 ${row.node.name}`"
            @click.stop="toggle(row.node)"
          >
            <component
              :is="isOpen(row.node) ? ChevronDown : ChevronRight"
              class="h-3.5 w-3.5"
              aria-hidden="true"
            />
          </button>
          <span v-else class="ml-0.5 h-6 w-5 shrink-0" aria-hidden="true" />

          <button
            type="button"
            class="flex min-w-0 flex-1 cursor-pointer items-center gap-1.5 py-1 pr-2 text-left text-body"
            :style="{ paddingLeft: `${row.depth * 12}px` }"
            :title="titleOf(row.node)"
            :aria-current="row.node.path === current ? 'true' : undefined"
            @click="open(row.node)"
          >
            <component
              :is="isOpen(row.node) ? FolderOpen : Folder"
              class="h-4 w-4 shrink-0"
              :class="row.node.path === current ? 'text-brand' : 'text-ink-weak'"
              aria-hidden="true"
            />
            <span
              class="truncate"
              :class="row.node.path === current ? 'font-semibold' : ''"
            >{{ row.node.name }}</span>
            <span class="ml-auto shrink-0 text-caption tabular-nums text-ink-weak">
              {{ formatCount(row.node.count) }}
            </span>
          </button>
        </div>
      </li>
    </ul>
  </nav>
</template>
