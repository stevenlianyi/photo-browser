<!--
  全站外壳：左侧栏 + 顶栏 + 内容区
  ------------------------------------------------------------
  布局用 flex + sticky 而不是 fixed + margin-left：
  固定偏移量在 <1024 侧栏收窄时要同步改两处样式，算错就是内容被压住一截。
-->
<script setup>
import { onBeforeUnmount, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import AppSidebar from '@/components/layout/AppSidebar.vue'
import AppTopbar from '@/components/layout/AppTopbar.vue'
import { useReviewStore } from '@/store/review'
import { useSettingsStore } from '@/store/settings'

const settings = useSettingsStore()
const review = useReviewStore()
const route = useRoute()

/** <1024 视口 → 侧栏收窄为图标（lg 断点 = 1024，取 max-width:1023px） */
const NARROW_QUERY = '(max-width: 1023px)'
let mediaQuery = null

function handleChange(event) {
  settings.setViewportNarrow(event.matches)
}

/**
 * 侧栏/顶栏角标：**必须有人去拉**，否则永远是 0（步骤 10 只把两个数放进 store，
 * 没人调用 fetchBadge —— 组件读得到、但值永远是初始值 0）。
 *
 * 拉取的时机：① 首屏（否则用户看到的第一眼就是 0，会以为队列是空的）；
 * ② 每次路由切换（后台扫描任务会往库里加未归属的脸，停在照片流翻半小时再回来看，
 * 角标还是旧数会让人不信任这个数字）。
 * 写操作（确认/改判）后**不再拉** —— 那些接口的响应里已经带回了新的两个数。
 */
async function refreshBadge() {
  try {
    await review.fetchBadge()
  } catch {
    // 角标拉不到不该打扰用户（request 层已 silent）；保留旧值即可
  }
}

onMounted(() => {
  refreshBadge()
  if (window.matchMedia) {
    mediaQuery = window.matchMedia(NARROW_QUERY)
    settings.setViewportNarrow(mediaQuery.matches)
    if (mediaQuery.addEventListener) {
      mediaQuery.addEventListener('change', handleChange)
    } else {
      mediaQuery.addListener(handleChange)
    }
  }
})

onBeforeUnmount(() => {
  if (!mediaQuery) return
  if (mediaQuery.removeEventListener) {
    mediaQuery.removeEventListener('change', handleChange)
  } else {
    mediaQuery.removeListener(handleChange)
  }
})

// 路由切换后刷新角标
watch(() => route.path, refreshBadge)
</script>

<template>
  <!-- 键盘用户第一个 Tab 停在这里（WCAG 2.4.1 绕过区块） -->
  <a class="pb-skip-link" href="#main">跳到主要内容</a>

  <!--
    Element Plus 的中文 locale（按需引入后不再有全局 install，用官方的
    el-config-provider 向下传）。少了它：日期选择器的月份 / 星期、空表格的
    「暂无数据」、分页文案会变回英文，而界面其它文案全是中文。
    ⚠️ 必须**祖先**于所有 EP 组件，所以放最外层；它是 renderless 的
       （只渲染默认插槽、不产生 DOM 节点），所以真正的布局根仍是下面那个 div。
  -->
  <el-config-provider :locale="zhCn">
    <div class="flex min-h-screen bg-bg">
      <AppSidebar />

      <div class="flex min-w-0 flex-1 flex-col">
        <AppTopbar />

        <main id="main" tabindex="-1" class="flex-1">
          <router-view />
        </main>
      </div>
    </div>
  </el-config-provider>
</template>