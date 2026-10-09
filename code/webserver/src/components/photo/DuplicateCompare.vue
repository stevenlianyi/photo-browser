<!--
  DuplicateCompare —— 重复照片并排对比（可选功能）
  ------------------------------------------------------------
  为什么需要它
  ------------
    照片流里有一个「重复照片」筛选，但它只能列出**哪些**重复，
    回答不了用户真正要问的那句：「这两张到底是不是同一张？该保留哪张？」。
    只给一个筛选入口等于半成品 —— 用户会自己开两个窗口比对，
    而比对时最需要的三样东西（内容是否逐字节相同 / 哪些元数据不同 /
    两边的人脸是不是被认成了不同的人）恰好是这个抽屉给的。

  为什么绝不提供「删掉其中一张」
  ------------------------------
    原图只读是设计红线（`photo\` 一个字节都不写）。这里出现的所有按钮
    都只改数据库（标记 / 取消重复），删除入口一个都没有。

  两种「重复」在库里长得一样
  --------------------------
    `isDuplicate=1` 既可能是「复制了一份」，也可能是「这其实是被改过名的同一张」，
    后者还有 `movedToPhotoCode` 双向链接（DR-11）。混着说会让用户去删错文件，
    所以分组时按 `kind` 分开显示。
-->
<script setup>
import { computed, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { CircleCheck, Copy, MoveRight, TriangleAlert } from 'lucide-vue-next'
import { compareDuplicates, listDuplicates } from '@/api/browse'
import { faceUrl, thumbUrl } from '@/api/static'
import { faceStateOf } from '@/utils/faceState'
import { baseName, formatCount, formatDate, shortHash } from '@/utils/format'
// 进照片详情要**声明来源**：详情页的「返回」才知道该回哪（见 utils/photoReturn.js）
import { photoDetailLink } from '@/utils/photoReturn'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue'])

const groups = ref([])
const total = ref(0)
const loading = ref(false)
const error = ref('')

const selected = ref(null)      // 选中的组
const compare = ref(null)      // /api/duplicates/compare 的响应
const compareLoading = ref(false)

const visible = computed({
  get: () => props.modelValue,
  set: (value) => emit('update:modelValue', value),
})

/**
 * DR-43：对比图的人工旋转样式。
 *
 * 两边都是固定 4:3 框 + `object-cover`、**没有人脸框**（人脸那一块用的是独立的
 * 裁剪图 `faceUrl()`，与照片方向无关，一律不转）。所以这里可以只转 img：
 * 90/270 时补一个 `scale(4/3)` 重新盖满，否则四角会露出卡片底色。
 * ⚠️ 角度从 `compare[side].rotateDeg` 取（`browse.photoSummary` 已透出）。
 */
function sideStyle(side) {
  const deg = ((Number(compare.value?.[side]?.rotateDeg) || 0) % 360 + 360) % 360
  if (!deg) return {}
  if (deg === 90 || deg === 270) return { transform: `rotate(${deg}deg) scale(4 / 3)` }
  return { transform: `rotate(${deg}deg)` }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await listDuplicates({ page: 1, size: 20 })
    groups.value = data?.items || []
    total.value = data?.total ?? 0
    if (!selected.value && groups.value.length) pick(groups.value[0])
  } catch (e) {
    error.value = e?.message || '读取重复分组失败'
    groups.value = []
  } finally {
    loading.value = false
  }
}

watch(visible, (value) => {
  if (value) {
    selected.value = null
    compare.value = null
    load()
  }
})

/** 组内任意选一张与第一张比（第一张是「本体」：它是 dupOfPhotoCode 的指向目标） */
async function pick(group) {
  selected.value = group
  compare.value = null
  const photos = group?.photos || []
  if (photos.length < 2) return
  compareLoading.value = true
  try {
    compare.value = await compareDuplicates(photos[0].photoCode, photos[1].photoCode)
  } catch (e) {
    compare.value = null
    error.value = e?.message || '对比失败'
  } finally {
    compareLoading.value = false
  }
}

/** 逐项不同的字段才高亮：全高亮等于没高亮 */
const changedFields = computed(() => {
  const out = {}
  for (const one of compare.value?.diff || []) out[one.field] = !one.same
  return out
})

function diffValue(one) {
  if (one.field === 'takenAt') return formatDate(one.left) + ' / ' + formatDate(one.right)
  if (one.field === 'width' || one.field === 'height' || one.field === 'fileSize') {
    return `${one.left ?? '—'} / ${one.right ?? '—'}`
  }
  return `${one.left || '—'} / ${one.right || '—'}`
}
</script>

<template>
  <el-drawer
    v-model="visible"
    title="重复照片对比"
    size="720px"
    @update:model-value="(value) => !value && (selected = null)"
  >
    <div class="space-y-4">
      <p class="pb-hint">
        判定的唯一依据是<b>文件内容指纹</b>（SHA-256），不是文件名/尺寸 ——
        改过名或重压过的图尺寸会变，但内容一样。两张图都在库里，
        本工具<b>不提供删除原图的任何入口</b>。
      </p>

      <p v-if="error" class="rounded-btn bg-danger-soft px-3 py-2 text-caption text-danger-ink" role="alert">
        {{ error }}
      </p>

      <div v-if="loading" class="space-y-2" aria-hidden="true">
        <div v-for="n in 3" :key="n" class="h-16 animate-pulse rounded-btn bg-skeleton" />
      </div>

      <p v-else-if="!groups.length" class="pb-card px-4 py-10 text-center text-body text-ink-weak">
        库里没有被判定为重复的照片。
      </p>

      <template v-else>
        <p class="pb-hint">共 {{ formatCount(total) }} 张被标记为重复，按内容指纹分组：</p>
        <ul class="space-y-2">
          <li v-for="group in groups" :key="group.dupOfPhotoCode">
            <button
              type="button"
              class="pb-card flex w-full items-center gap-3 p-2 text-left transition-colors duration-150 hover:bg-surface"
              :class="selected?.dupOfPhotoCode === group.dupOfPhotoCode ? 'border-brand' : ''"
              :aria-pressed="selected?.dupOfPhotoCode === group.dupOfPhotoCode"
              @click="pick(group)"
            >
              <span class="flex -space-x-2">
                <img
                  v-for="photo in group.photos.slice(0, 3)"
                  :key="photo.photoCode"
                  :src="thumbUrl(photo.photoCode, 200)"
                  :alt="baseName(photo.relPath)"
                  class="h-11 w-11 rounded-thumb border-2 border-card object-cover"
                  loading="lazy"
                />
              </span>
              <span class="min-w-0 flex-1">
                <span class="block truncate text-body text-ink">
                  {{ baseName(group.photos[0]?.relPath) }}
                  <span v-if="group.photos.length > 1" class="text-ink-weak">
                    等 {{ group.photos.length }} 份
                  </span>
                </span>
                <span class="block truncate text-caption text-ink-weak">
                  {{ group.photos.map((one) => baseName(one.relPath)).join(' · ') }}
                </span>
              </span>
              <span
                class="inline-flex shrink-0 items-center gap-1 rounded-btn px-2 py-0.5 text-caption"
                :class="group.kind === 'moved'
                  ? 'bg-info-soft text-info-ink'
                  : 'bg-warning-soft text-warning-ink'"
              >
                <component
                  :is="group.kind === 'moved' ? MoveRight : Copy"
                  class="h-3 w-3"
                  aria-hidden="true"
                />
                {{ group.kind === 'moved' ? '疑似改名 / 移动' : '复制了一份' }}
              </span>
            </button>
            <p v-if="group.kind === 'moved'" class="pb-hint mt-1 px-2">
              这组是<b>同一份文件换了个路径</b>（旧记录已不在磁盘上）——
              库里的两条记录都保留着，<b>不会自动改路径</b>，由你核对。
            </p>
          </li>
        </ul>

        <!-- 并排对比 -->
        <section v-if="selected" class="pb-card p-3" aria-label="并排对比">
          <div v-if="compareLoading" class="pb-hint">正在比对…</div>
          <template v-else-if="compare">
            <p
              class="mb-3 flex items-center gap-2 rounded-btn px-3 py-2 text-caption"
              :class="
                compare.sameContent
                  ? 'bg-success-soft text-success-ink'
                  : 'bg-warning-soft text-warning-ink'
              "
            >
              <component
                :is="compare.sameContent ? CircleCheck : TriangleAlert"
                class="h-3.5 w-3.5"
                aria-hidden="true"
              />
              <template v-if="compare.sameContent">
                <b>内容逐字节相同</b> —— 这是同一份文件的两个副本。
              </template>
              <template v-else>
                <b>内容不同</b>（只是被标记成同一组，可能经历过重压缩或换过内容）。
              </template>
            </p>

            <div class="grid grid-cols-2 gap-3">
              <figure v-for="side in ['left', 'right']" :key="side">
                <RouterLink
                  :to="photoDetailLink(compare[side].photoCode, { path: '/photos', name: '照片流' })"
                  class="pb-photo-frame block aspect-[4/3]"
                >
                  <img
                    :src="thumbUrl(compare[side].photoCode, 400)"
                    :alt="baseName(compare[side].relPath)"
                    class="h-full w-full object-cover"
                    :style="sideStyle(side)"
                    loading="lazy"
                  />
                </RouterLink>
                <figcaption class="mt-1 truncate text-caption text-ink-sub" :title="compare[side].relPath">
                  {{ baseName(compare[side].relPath) }}
                </figcaption>
                <p class="truncate font-mono text-caption text-ink-weak" :title="compare[side].relPath">
                  {{ compare[side].relPath }}
                </p>
                <p class="truncate font-mono text-caption text-ink-weak">
                  sha256:{{ shortHash(compare[side].fileHash) }}
                </p>
              </figure>
            </div>

            <!-- 逐项差异：只把不同的项标出来 -->
            <table class="mt-3 w-full text-caption">
              <caption class="sr-only">两张照片的元数据逐项对比</caption>
              <thead>
                <tr class="text-left text-ink-weak">
                  <th scope="col" class="py-1">项</th>
                  <th scope="col" class="py-1">值</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="one in compare.diff" :key="one.field">
                  <th scope="row" class="py-1 pr-2 text-left font-normal text-ink-weak">
                    {{ one.label }}
                  </th>
                  <td
                    class="py-1 tabular-nums"
                    :class="changedFields[one.field] ? 'text-warning-ink' : 'text-ink-sub'"
                  >
                    {{ diffValue(one) }}
                  </td>
                </tr>
              </tbody>
            </table>

            <!-- 人脸配对：重复照片会「放大」错分 -->
            <div v-if="compare.faces?.left?.length || compare.faces?.right?.length" class="mt-4">
              <h4 class="text-body text-ink">人脸对照</h4>
              <p class="pb-hint mt-1">
                同一份内容在库里存成两条记录时，**两边的人脸会各归属一次** ——
                这正是重复照片会放大错分的原因。下面按「归属到谁」配对。
              </p>
              <div class="mt-2 grid grid-cols-2 gap-3">
                <ul class="space-y-2">
                  <li v-for="one in compare.faces.left" :key="one.faceCode" class="flex items-center gap-2">
                    <img
                      :src="faceUrl(one.faceCode)"
                      :alt="one.faceCode"
                      class="h-9 w-9 rounded-full border object-cover"
                      :class="{
                        'border-success border-solid': one.state === 'confirmed',
                        'border-success border-dashed': one.state === 'disputed',
                        'border-[var(--pb-warning-line)] border-dotted': one.state === 'pending',
                      }"
                      loading="lazy"
                    />
                    <span class="text-caption text-ink-sub">
                      {{ one.personCode || '未归属' }}
                      <span aria-hidden="true">{{ faceStateOf(one).icon }}</span>
                    </span>
                  </li>
                </ul>
                <ul class="space-y-2">
                  <li v-for="one in compare.faces.right" :key="one.faceCode" class="flex items-center gap-2">
                    <img
                      :src="faceUrl(one.faceCode)"
                      :alt="one.faceCode"
                      class="h-9 w-9 rounded-full border object-cover"
                      :class="{
                        'border-success border-solid': one.state === 'confirmed',
                        'border-success border-dashed': one.state === 'disputed',
                        'border-[var(--pb-warning-line)] border-dotted': one.state === 'pending',
                      }"
                      loading="lazy"
                    />
                    <span class="text-caption text-ink-sub">
                      {{ one.personCode || '未归属' }}
                      <span aria-hidden="true">{{ faceStateOf(one).icon }}</span>
                    </span>
                  </li>
                </ul>
              </div>
              <p
                v-if="compare.faces.paired.some((one) => !one.consistent)"
                class="mt-2 rounded-btn bg-warning-soft px-3 py-2 text-caption text-warning-ink"
              >
                有人的脸在两张里被认成了<b>不同的归属</b> —— 这是重复照片放大错分的直接证据，
                建议到照片详情里改判。
              </p>
            </div>
          </template>
        </section>
      </template>
    </div>
  </el-drawer>
</template>