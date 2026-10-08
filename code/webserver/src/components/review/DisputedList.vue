<!--
  DisputedList —— 「我不同意」列表（设计稿 §4.6 Tab2 + DR-16②）
  ------------------------------------------------------------
  这一页是**用户浏览时发现认错后的申诉入口**，所以它的组织方式必须
  与「待确认」相反：

    待确认   = 一张脸一张脸地看（并排比对未知人脸 vs 候选头像）
    我不同意 = 一次看**一整张照片**（「这张照片认错了」通常是整体判断：
              机器把这张合影里的所有人都认成了同一个不存在的人）

  两条硬指标
  ----------
  ① **整张一键否决 ≤2 次点击**：第一次点「整张否决」，第二次点确认。
     用 popconfirm 而不是 el-dialog —— dialog 要等渲染，第三次点击才点得到；
     popconfirm 挂在按钮旁边，第二次点击落点固定。
  ② **每条都要写清「机器认成谁、相似度多少」**
     只给一张缩略图和一个「否决」按钮，用户无法判断这是不是他要找的那条。
     没有这句话，这个列表就只是一堆没法核对的缩略图。

  改判之后这些脸去哪
  ------------------
  「否决」= fix('unknown')，脸退回 **personCode 为空** → 进「待确认」Tab。
  「改判到某人」= fix('assign')，脸变成人工确认归属 → 从本列表消失。
  两种结果都不再是「机器认的」，所以离开这个列表是对的，不是丢数据。
-->
<script setup>
import { Layers, Split, X } from 'lucide-vue-next'
import { RouterLink } from 'vue-router'
import { faceUrl, thumbUrl } from '@/api/static'
import { similarityText } from '@/utils/faceState'
// 进照片详情要**声明来源**：详情页的「返回」才知道该回哪（见 utils/photoReturn.js）
import { photoDetailLink } from '@/utils/photoReturn'

defineProps({
  /** /api/review/disputed 的 items（groupByPhoto=1 的分组结构） */
  groups: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
  /** 正在处理中的 photoCode（避免重复点） */
  busyPhotoCode: { type: String, default: '' },
})

const emit = defineEmits(['reject-group', 'reject-face', 'fix', 'split', 'batch-cluster'])

function facesOf(group) {
  return Array.isArray(group?.faces) ? group.faces : []
}

/** 组内涉及的机器归属人（可能多个：合影里各认成不同的人） */
function ownersOf(group) {
  const names = new Set()
  for (const face of facesOf(group)) {
    if (face.displayName) names.add(face.displayName)
  }
  return [...names]
}

/** 簇内批量改判：组里所有脸同属一簇时给这个入口（一次点击解决 N 张） */
function clusterOf(group) {
  const codes = new Set(facesOf(group).map((f) => f.clusterCode).filter(Boolean))
  return codes.size === 1 ? [...codes][0] : ''
}

function photoLabel(group) {
  const code = String(group?.photoCode || '')
  return code.length > 14 ? `…${code.slice(-12)}` : code
}

function ownerName(group) {
  return ownersOf(group).join('、') || '某人'
}
</script>

<template>
  <div class="space-y-3">
    <p class="pb-hint">
      这些脸是<b>机器自动认的</b>、还没经你确认 —— 浏览照片时看到认错，
      先来这里整张否决（否决后它们会回到「待确认」Tab）。
    </p>

    <p
      v-if="!groups.length && !loading"
      class="pb-card px-4 py-8 text-center text-body text-ink-weak"
    >
      暂无「我不同意」的条目 —— 说明当前没有被机器自动归属、却还没你确认的脸。
    </p>

    <article v-for="group in groups" :key="group.photoCode" class="pb-card p-3">
      <div class="flex flex-wrap items-start gap-3">
        <RouterLink
          :to="photoDetailLink(group.photoCode, { path: '/review', name: '待确认' })"
          class="shrink-0"
          :aria-label="`打开照片 ${photoLabel(group)}`"
        >
          <img
            :src="thumbUrl(group.photoCode, 200)"
            class="h-16 w-16 rounded-thumb border border-line object-cover"
            :alt="`照片 ${photoLabel(group)} 的缩略图`"
            loading="lazy"
            decoding="async"
          />
        </RouterLink>

        <div class="min-w-0 flex-1">
          <p class="flex flex-wrap items-center gap-x-2 text-body text-ink">
            <RouterLink
              :to="photoDetailLink(group.photoCode, { path: '/review', name: '待确认' })"
              class="truncate font-mono text-caption hover:underline"
              >{{ photoLabel(group) }}</RouterLink
            >
            <span class="text-caption text-ink-weak">
              · {{ facesOf(group).length }} 张脸 · 机器认成
              <b class="text-ink-sub">{{ ownerName(group) }}</b>
            </span>
          </p>

          <!-- 每一条：机器认成谁 + 相似度 + 去改判（不只给一个缩略图） -->
          <ul class="mt-2 space-y-1.5">
            <li
              v-for="face in facesOf(group)"
              :key="face.faceCode"
              class="flex flex-wrap items-center gap-2 rounded-btn bg-surface px-2 py-1.5"
            >
              <img
                v-if="face.avatarFaceCode"
                :src="faceUrl(face.avatarFaceCode)"
                class="h-6 w-6 shrink-0 rounded-full border border-line object-cover"
                alt=""
              />
              <span
                v-else
                class="h-6 w-6 shrink-0 rounded-full border border-line bg-card"
                aria-hidden="true"
              />
              <span class="min-w-0 flex-1 truncate text-caption text-ink-sub">
                机器认成
                <b class="text-ink">{{ face.displayName || '某人' }}</b>，相似度
                <b class="tabular-nums text-ink">{{ similarityText(face.similarity) }}</b>
                <span class="sr-only">（未经人工确认，可否决）</span>
              </span>
              <el-button size="small" text @click="emit('fix', face)">去改判</el-button>
              <!--
                「拆分」= 换个主人（走 POST /review/split，**记「谁本来属于谁」且可撤销**）。
                「否决」= 丢回待确认队列（不记归属，且不可撤销）。

                ⚠️ 两个按钮的区别必须在 tooltip 里写清楚，而不是靠图标猜 ——
                库里已经有「否决」（popconfirm）和「整张否决」，再塞一个含义相近的
                「拆分」却不解释，误点一次的代价是丢掉一条归属线索。
                ⚠️ 入口在手边（不藏三级菜单），设计稿明确要求。
              -->
              <el-tooltip
                placement="top"
                content="换个主人：真拆走并记下「它本来属于谁」，可撤销。不确定去处分就用「否决」。"
              >
                <el-button
                  size="small"
                  text
                  :aria-label="`把这张脸从 ${face.displayName || '某人'} 名下拆出来，另选一个主人`"
                  @click="emit('split', face)"
                >
                  <Split class="mr-1 h-3.5 w-3.5" aria-hidden="true" />拆分
                </el-button>
              </el-tooltip>
              <el-popconfirm
                :title="`否决这张脸（认成 ${face.displayName || '某人'}）？它会回到「待确认」队列`"
                confirm-button-text="否决"
                cancel-button-text="取消"
                confirm-button-type="danger"
                :width="260"
                @confirm="emit('reject-face', face)"
              >
                <template #reference>
                  <el-button
                    size="small"
                    text
                    type="danger"
                    :loading="busyPhotoCode === group.photoCode"
                    :aria-label="`否决这张被认成 ${face.displayName || '某人'} 的脸`"
                  >
                    <X class="mr-1 h-3.5 w-3.5" aria-hidden="true" />否决
                  </el-button>
                </template>
              </el-popconfirm>
            </li>
          </ul>
        </div>

        <!-- 组级操作：整张否决（≤2 次点击）/ 簇内批量改判 -->
        <div class="flex shrink-0 flex-col items-stretch gap-2">
          <el-popconfirm
            :title="`把这张照片里 ${facesOf(group).length} 张脸全部否决？它们会回到「待确认」队列`"
            confirm-button-text="整张否决"
            cancel-button-text="取消"
            confirm-button-type="danger"
            :width="280"
            @confirm="emit('reject-group', group)"
          >
            <template #reference>
              <el-button
                type="danger"
                plain
                :loading="busyPhotoCode === group.photoCode"
                :aria-label="`整张否决：${photoLabel(group)} 里的 ${facesOf(group).length} 张脸`"
              >
                整张否决
              </el-button>
            </template>
          </el-popconfirm>

          <el-button
            v-if="clusterOf(group)"
            :aria-label="`按聚类簇批量改判 ${clusterOf(group)}`"
            @click="emit('batch-cluster', clusterOf(group))"
          >
            <Layers class="mr-1 h-3.5 w-3.5" aria-hidden="true" />簇内批量
          </el-button>
        </div>
      </div>
    </article>
  </div>
</template>
