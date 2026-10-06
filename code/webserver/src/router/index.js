/* ============================================================
 * 路由表 —— 8 个页面（对齐设计稿 §3.3 页面清单）
 * ============================================================
 * 为什么不用嵌套路由：App.vue 里直接放 <router-view>，
 * 侧栏/顶栏是全站常驻外壳，不参与路由切换 —— 省掉一层无意义的包装组件。
 *
 * meta 约定（AppTopbar / AppSidebar 读，不在组件里各写一份文案）：
 *   title       页面标题（顶栏与 <title>）
 *   crumbs      面包屑数组，末项是当前页
 *   icon        lucide-vue-next 图标组件名（字符串，Sidebar 用 resolveDynamicComponent）
 *   badge       需要显示侧栏角标计数（review 页面对应两个计数）
 * ============================================================ */
import { createRouter, createWebHistory } from 'vue-router'

/**
 * 侧栏一级导航，固定 6 项（设计稿 §3.1）。
 * 顺序与 §4.1 的 ASCII 布局一致：设置用 divider 隔开，固定在底部。
 */
export const NAV_ITEMS = [
  { name: 'overview', path: '/', label: '概览', icon: 'Gauge' },
  { name: 'photos', path: '/photos', label: '照片流', icon: 'Images' },
  { name: 'people', path: '/people', label: '人物库', icon: 'Users' },
  { name: 'review', path: '/review', label: '待确认', icon: 'ClipboardCheck', badge: true },
  { name: 'scanJobs', path: '/scan-jobs', label: '扫描任务', icon: 'ScanLine' },
  { name: 'settings', path: '/settings', label: '设置', icon: 'Settings', divider: true },
]

const routes = [
  {
    path: '/',
    name: 'overview',
    component: () => import('@/views/OverviewView.vue'),
    meta: { title: '概览', crumbs: ['概览'], icon: 'Gauge' },
  },
  {
    path: '/photos',
    name: 'photos',
    component: () => import('@/views/PhotosView.vue'),
    meta: { title: '照片流', crumbs: ['照片流'], icon: 'Images' },
  },
  {
    path: '/photos/:photoCode',
    name: 'photoDetail',
    component: () => import('@/views/PhotoDetailView.vue'),
    meta: { title: '照片详情', crumbs: ['照片流', '照片详情'], icon: 'Image' },
  },
  {
    path: '/people',
    name: 'people',
    component: () => import('@/views/PeopleView.vue'),
    meta: { title: '人物库', crumbs: ['人物库'], icon: 'Users' },
  },
  {
    path: '/people/:personCode',
    name: 'personDetail',
    component: () => import('@/views/PersonDetailView.vue'),
    meta: { title: '人物详情', crumbs: ['人物库', '人物详情'], icon: 'User' },
  },
  {
    path: '/review',
    name: 'review',
    component: () => import('@/views/ReviewView.vue'),
    meta: { title: '待确认', crumbs: ['待确认'], icon: 'ClipboardCheck', badge: true },
  },
  {
    path: '/scan-jobs',
    name: 'scanJobs',
    component: () => import('@/views/ScanJobsView.vue'),
    meta: { title: '扫描任务', crumbs: ['扫描任务'], icon: 'ScanLine' },
  },
  {
    path: '/settings',
    name: 'settings',
    component: () => import('@/views/SettingsView.vue'),
    meta: { title: '设置', crumbs: ['设置'], icon: 'Settings' },
  },
  // 未匹配路径回概览：本机单用户工具，不做 404 页
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes,
  // 侧栏/顶栏依赖当前路由做高亮与面包屑，用 history 模式即可；
  // 步骤 11 接数据后再考虑滚动行为（scrollBehavior）
})

const BASE_TITLE = 'photo-browser'

router.afterEach((to) => {
  const title = to.meta?.title
  document.title = title ? `${title} · ${BASE_TITLE}` : BASE_TITLE
})

export default router